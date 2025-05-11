from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import sqlite3
import os
import re
import logging
import json
import unicodedata
from itertools import combinations

# 設置日誌
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='../frontend')
CORS(app)

# 資料庫路徑
DB_PATH = os.path.join(os.path.dirname(__file__), 'crop_usage.db')

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def normalize_pest_name(name):
    if not name:
        return ''
    # 去除空白、全半形、轉小寫
    name = unicodedata.normalize('NFKC', name)
    name = name.replace(' ', '').replace('\u3000', '').lower()
    return name

def remove_duplicate_pesticides(pesticides):
    """去除重複的農藥（中文名稱+劑型+含量）"""
    unique_pesticides = {}
    for pesticide in pesticides:
        key = f"{pesticide['中文名稱']}__{pesticide['劑型']}__{pesticide['含量']}"
        if key not in unique_pesticides:
            unique_pesticides[key] = pesticide
    return list(unique_pesticides.values())

def remove_duplicate_usages(usages):
    """去除重複的使用資訊"""
    unique_usages = []
    for usage in usages:
        if not any(
            u['病蟲害名稱'] == usage['病蟲害名稱'] and
            u['安全採收期'] == usage['安全採收期'] and
            u['稀釋倍數'] == usage['稀釋倍數'] and
            u['每公頃使用用藥量'] == usage['每公頃使用用藥量']
            for u in unique_usages
        ):
            unique_usages.append(usage)
    return unique_usages

@app.route('/')
def index():
    return send_from_directory('../frontend', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('../frontend', path)

@app.route('/api/search')
def search_pesticides():
    try:
        keywords = request.args.get('keywords', '')
        logger.debug(f"收到搜尋請求，關鍵字: {keywords}")

        if not keywords:
            return jsonify([])

        keyword_list = [k.strip() for k in re.split(r'[,，\s]+', keywords) if k.strip()]
        logger.debug(f"處理後的關鍵字列表: {keyword_list}")

        if not keyword_list:
            return jsonify([])

        conn = get_db_connection()
        cursor = conn.cursor()

        # 取得所有類型名稱集合
        cursor.execute("SELECT DISTINCT 作物名稱 FROM crop_usage")
        all_crops = set(row['作物名稱'] for row in cursor.fetchall())
        cursor.execute("SELECT DISTINCT 病蟲害名稱 FROM crop_usage")
        all_pests = set(row['病蟲害名稱'] for row in cursor.fetchall())
        cursor.execute("SELECT DISTINCT 中文名稱 FROM crop_usage")
        all_chems = set(row['中文名稱'] for row in cursor.fetchall())
        cursor.execute("SELECT DISTINCT 廠牌名稱 FROM crop_usage")
        all_brands = set(row['廠牌名稱'] for row in cursor.fetchall())
        cursor.execute("SELECT DISTINCT 條碼 FROM crop_usage")
        all_barcodes = set(row['條碼'] for row in cursor.fetchall())

        # 分類
        crop_keywords = [kw for kw in keyword_list if kw in all_crops]
        pest_keywords = [kw for kw in keyword_list if kw in all_pests]
        chem_keywords = [kw for kw in keyword_list if kw in all_chems]
        brand_keywords = [kw for kw in keyword_list if kw in all_brands]
        barcode_keywords = [kw for kw in keyword_list if kw in all_barcodes]
        other_keywords = [kw for kw in keyword_list if kw not in (
            all_crops | all_pests | all_chems | all_brands | all_barcodes
        )]
        mixed_keywords = chem_keywords + brand_keywords + barcode_keywords

        results = []

        # 作物 + 多個病蟲害（交集）
        if crop_keywords and len(pest_keywords) >= 2:
            results += handle_crop_pests_intersection(cursor, crop_keywords, pest_keywords)

        # 作物 + 單一病蟲害
        if crop_keywords and len(pest_keywords) == 1:
            results += handle_crop_single_pest(cursor, crop_keywords, pest_keywords[0])

        # 作物 + 農藥 / 廠牌 / 條碼（逐個查）
        if crop_keywords and mixed_keywords:
            results += handle_crop_mixed_keywords(
                cursor, crop_keywords, mixed_keywords, all_chems, all_brands, all_barcodes
            )

        # 只有作物（無其他關鍵字）
        if crop_keywords and not (pest_keywords or mixed_keywords):
            results += handle_crop_only(cursor, crop_keywords)

        # 只有農藥
        if chem_keywords and not crop_keywords:
            results += handle_chem_only(cursor, chem_keywords)

        # 只有廠牌
        if brand_keywords and not crop_keywords:
            results += handle_brand_only(cursor, brand_keywords)

        # 只有條碼
        if barcode_keywords and not crop_keywords:
            results += handle_barcode_only(cursor, barcode_keywords)

        # 其他 fallback 模糊查詢（只有其他未分類詞）
        if not crop_keywords and not pest_keywords and not mixed_keywords and other_keywords:
            results += handle_fallback_partial_match(cursor, other_keywords)

        # 排序與回傳
        results = deduplicate_and_sort_results(results, crop_keywords, pest_keywords, keyword_list)
        conn.close()

        # 組合最終結果
        final_results = results

        # 將所有結果中的 matchSet 從 set 轉換為 list
        for result in final_results:
            if 'matchSet' in result:
                result['matchSet'] = list(result['matchSet'])

        print(f"處理請求完成，共找到 {len(final_results)} 個結果")
        return jsonify(final_results)

    except Exception as e:
        logger.error(f"處理請求時發生錯誤: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

def deduplicate_and_sort_results(results, crop_keywords, pest_keywords, keywords=None):
    def extract_days(text):
        if not text:
            return float('inf')
        try:
            return int(re.sub(r'\D', '', text))
        except:
            return float('inf')

    # 依 has_exact_match、交集數量、安全採收期排序
    results.sort(key=lambda x: (
        -x.get('has_exact_match', False) if isinstance(x, dict) else 0,
        extract_days(next((u['安全採收期'] for u in x.get('usages', []) if u['安全採收期']), '')) if isinstance(x, dict) else float('inf')
    ))
    return results

#1. 作物 + 多個病蟲害（交集）卡片
def handle_crop_pests_intersection(cursor, crop_keywords, pest_keywords):
    results = []
    for crop in crop_keywords:
        all_pesticides = set()
        for pest in pest_keywords:
            cursor.execute("SELECT DISTINCT 中文名稱, 劑型, 含量 FROM crop_usage WHERE 作物名稱 = ? AND 病蟲害名稱 LIKE ?", (crop, f"%{pest}%"))
            rows = cursor.fetchall()
            pesticides = {f"{row['中文名稱']}__{row['劑型']}__{row['含量']}" for row in rows}
            if not all_pesticides:
                all_pesticides = pesticides
            else:
                all_pesticides = all_pesticides.intersection(pesticides)
        if not all_pesticides:
            results.append({
                'no_match': True,
                'error_message': f"無共同防治藥劑：{crop}的{', '.join(pest_keywords)}",
                'crop': crop,
                'keyword': ', '.join(pest_keywords)
            })
        else:
            all_rows = []
            for pest in pest_keywords:
                cursor.execute("SELECT * FROM crop_usage WHERE 作物名稱 = ? AND 病蟲害名稱 LIKE ?", (crop, f"%{pest}%"))
                all_rows.extend(cursor.fetchall())
            unique_pesticides = [
                p for p in remove_duplicate_pesticides([{
                    '中文名稱': row['中文名稱'],
                    '劑型': row['劑型'],
                    '含量': row['含量'],
                    '作用機制名稱': row['作用機制名稱'],
                    '作用機制備註': row['作用機制備註'],
                    'usages': [],
                    'has_common_pesticide': True,
                    'crop': crop,
                    'keyword': ', '.join(pest_keywords),
                    'is_crop_and_pest_search': True
                } for row in all_rows])
                if f"{p['中文名稱']}__{p['劑型']}__{p['含量']}" in all_pesticides
            ]
            for pesticide in unique_pesticides:
                for row in all_rows:
                    if (row['中文名稱'] == pesticide['中文名稱'] and 
                        row['劑型'] == pesticide['劑型'] and 
                        row['含量'] == pesticide['含量']):
                        pesticide['usages'].append({
                            '作物名稱': row['作物名稱'],
                            '病蟲害名稱': row['病蟲害名稱'],
                            '病蟲害名稱_normalized': normalize_pest_name(row['病蟲害名稱']),
                            '安全採收期': row['安全採收期'] or '',
                            '稀釋倍數': row['稀釋倍數'] or '',
                            '每公頃使用用藥量': row['每公頃使用用藥量'] or ''
                        })
                pesticide['usages'] = remove_duplicate_usages(pesticide['usages'])
            results.extend(unique_pesticides)
    return results

#2.作物+單一病害卡片
def handle_crop_single_pest(cursor, crop_keywords, pest):
    results = []
    for crop in crop_keywords:
        cursor.execute("SELECT * FROM crop_usage WHERE 作物名稱 = ? AND 病蟲害名稱 LIKE ?", (crop, f"%{pest}%"))
        rows = cursor.fetchall()
        if not rows:
            results.append({
                'no_match': True,
                'error_message': f"無登記使用防治藥劑：{crop}的{pest}",
                'crop': crop,
                'keyword': pest
            })
        else:
            pesticide_map = {}
            for row in rows:
                key = f"{row['中文名稱']}__{row['劑型']}__{row['含量']}"
                if key not in pesticide_map:
                    pesticide_map[key] = {
                        '中文名稱': row['中文名稱'],
                        '劑型': row['劑型'],
                        '含量': row['含量'],
                        '作用機制名稱': row['作用機制名稱'] or '',
                        '作用機制備註': row['作用機制備註'] or '',
                        'usages': [],
                        'crop': crop,
                        'keyword': pest,
                        'is_crop_and_pest_search': True
                    }
                pesticide_map[key]['usages'].append({
                    '作物名稱': row['作物名稱'],
                    '病蟲害名稱': row['病蟲害名稱'],
                    '病蟲害名稱_normalized': normalize_pest_name(row['病蟲害名稱']),
                    '安全採收期': row['安全採收期'] or '',
                    '稀釋倍數': row['稀釋倍數'] or '',
                    '每公頃使用用藥量': row['每公頃使用用藥量'] or ''
                })
            for pesticide in pesticide_map.values():
                pesticide['usages'] = remove_duplicate_usages(pesticide['usages'])
            results.extend(pesticide_map.values())
    return results

#3作物+中文名稱(農藥)/廠牌名稱/條碼卡片
def handle_crop_mixed_keywords(cursor, crop_keywords, mixed_keywords, all_chems, all_brands, all_barcodes):
    results = []
    # 先收集所有病蟲害名稱，按農藥中文名稱分組
    pest_names_by_chem = {}
    
    # 第一步：收集所有病蟲害名稱
    for crop in crop_keywords:
        for kw in mixed_keywords:
            if kw in all_chems:
                cursor.execute("SELECT * FROM crop_usage WHERE 作物名稱 = ? AND 中文名稱 = ?", (crop, kw))
                kw_type = 'chem'
            elif kw in all_brands:
                cursor.execute("SELECT * FROM crop_usage WHERE 作物名稱 = ? AND 廠牌名稱 = ?", (crop, kw))
                kw_type = 'brand'
            elif kw in all_barcodes:
                cursor.execute("SELECT * FROM crop_usage WHERE 作物名稱 = ? AND 條碼 = ?", (crop, kw))
                kw_type = 'barcode'
            else:
                continue
            rows = cursor.fetchall()
            if not rows:
                # 只在這裡插入特殊卡片
                if kw_type == 'chem':
                    display_name = kw
                    error_message = f"{display_name} 無法使用於作物：{crop}"
                elif kw_type == 'brand':
                    cursor.execute("SELECT DISTINCT 中文名稱 FROM crop_usage WHERE 廠牌名稱 = ?", (kw,))
                    chems = [row['中文名稱'] for row in cursor.fetchall()]
                    if chems:
                        display_name = f"{chems[0]}（{kw}）"
                    else:
                        display_name = f"（{kw}）"
                    error_message = f"{display_name} 無法使用於作物：{crop}"
                elif kw_type == 'barcode':
                    cursor.execute("SELECT 中文名稱, 廠牌名稱 FROM crop_usage WHERE 條碼 = ?", (kw,))
                    row = cursor.fetchone()
                    if row:
                        display_name = f"{row['中文名稱']}（{row['廠牌名稱']}） 條碼：{kw}"
                    else:
                        display_name = f"條碼：{kw}"
                    error_message = f"{display_name} 無法使用於作物：{crop}"
                else:
                    display_name = kw
                    error_message = f"{display_name} 無法使用於作物：{crop}"
                results.insert(0, {
                    'no_match': True,
                    'error_message': error_message,
                    'crop': crop,
                    'keyword': kw,
                    'query_type': f"作物+農藥" if kw_type == 'chem' else f"作物+{kw_type}"
                })
                continue

            # 收集病蟲害名稱
            for row in rows:
                chem_name = row['中文名稱']  # 使用原始中文名稱
                if chem_name not in pest_names_by_chem:
                    pest_names_by_chem[chem_name] = set()
                pest_names_by_chem[chem_name].add(row['病蟲害名稱'])
                print(f"收集病蟲害名稱: 農藥={chem_name}, 病蟲害={row['病蟲害名稱']}")

            # 下面這段是原本的資料組裝，不要動
            pesticide_map = {}
            for row in rows:
                key = f"{row['中文名稱']}__{row['劑型']}__{row['含量']}"
                if kw_type == 'brand':
                    display_name = f"{row['中文名稱']}（{row['廠牌名稱']}）"
                elif kw_type == 'barcode':
                    display_name = f"{row['中文名稱']}（{row['廠牌名稱']}） 條碼：{row['條碼']}"
                else:
                    display_name = row['中文名稱']
                if key not in pesticide_map:
                    pesticide_map[key] = {
                        '中文名稱': display_name,
                        'raw_chem_name': row['中文名稱'],  # 加入原始中文名稱
                        '劑型': row['劑型'],
                        '含量': row['含量'],
                        '作用機制名稱': row['作用機制名稱'] or '',
                        '作用機制備註': row['作用機制備註'] or '',
                        'usages': [],
                        'crop': crop,
                        'keyword': kw,
                        'is_crop_and_pest_search': False,  # 因為這是作物+農藥/廠牌/條碼的搜尋，不是作物+病蟲害
                        'query_type': f"作物+農藥" if kw_type == 'chem' else f"作物+{kw_type}"
                    }
                pesticide_map[key]['usages'].append({
                    '作物名稱': row['作物名稱'],
                    '病蟲害名稱': row['病蟲害名稱'],
                    '病蟲害名稱_normalized': normalize_pest_name(row['病蟲害名稱']),
                    '安全採收期': row['安全採收期'] or '',
                    '稀釋倍數': row['稀釋倍數'] or '',
                    '每公頃使用用藥量': row['每公頃使用用藥量'] or ''
                })
            for pesticide in pesticide_map.values():
                pesticide['usages'] = remove_duplicate_usages(pesticide['usages'])
            results.extend(pesticide_map.values())

    # 第二步：建立重複病蟲害名稱的字典
    duplicate_pests = set()

    # 比較不同中文名稱農藥之間的病蟲害名稱
    chem_names = list(pest_names_by_chem.keys())
    for i in range(len(chem_names)):
        for j in range(i + 1, len(chem_names)):
            chem1 = chem_names[i]
            chem2 = chem_names[j]
            pests1 = pest_names_by_chem.get(chem1, [])
            pests2 = pest_names_by_chem.get(chem2, [])

            if not pests1 or not pests2:
                continue

            common_pests = set()
            for pest1 in pests1:
                for pest2 in pests2:
                    if (
                        pest1 == pest2 or
                        pest1 in pest2 or
                        pest2 in pest1 or
                        pest1.replace('葉', '') == pest2.replace('葉', '')
                    ):
                        common_pests.add(pest1)
                        common_pests.add(pest2)

            duplicate_pests.update(common_pests)

    print(f"\n需要標記的病蟲害名稱: {duplicate_pests}")

    # 第三步：標記重複的病蟲害名稱
    marked_count = 0
    for result in results:
        if isinstance(result, dict) and not result.get('no_match'):
            for usage in result.get('usages', []):
                pest_name = usage.get('病蟲害名稱', '')
                if pest_name in duplicate_pests:
                    usage['病蟲害名稱'] = f"#{pest_name}"
                    marked_count += 1
                    print(f"標記病蟲害: {pest_name} 在農藥 {result.get('raw_chem_name')} 中")

    print(f"\n總共標記了 {marked_count} 個病蟲害名稱")

    # 只有在符合條件時才添加提示卡片：
    # 1. 是作物+中文名稱(農藥)/廠牌名稱/條碼卡片的搜尋
    # 2. 搜尋的關鍵字數量 >= 2
    if marked_count == 0 and len(mixed_keywords) >= 2:
        results.insert(0, {
            'no_match': True,
            'error_message': '沒有重複防治的害物',
            'crop': crop_keywords[0] if crop_keywords else '',
            'keyword': ', '.join(mixed_keywords)
        })
        print("無共同防治的病害，已添加提示卡片")

    return results

#4.只有作物的卡片
def handle_crop_only(cursor, crop_keywords):
    results = []
    for crop in crop_keywords:
        cursor.execute("SELECT * FROM crop_usage WHERE 作物名稱 = ?", (crop,))
        rows = cursor.fetchall()
        pesticide_map = {}
        for row in rows:
            key = f"{row['中文名稱']}__{row['劑型']}__{row['含量']}"
            if key not in pesticide_map:
                pesticide_map[key] = {
                    '中文名稱': row['中文名稱'],
                    '劑型': row['劑型'],
                    '含量': row['含量'],
                    '作用機制名稱': row['作用機制名稱'] or '',
                    '作用機制備註': row['作用機制備註'] or '',
                    'usages': []
                }
            pesticide_map[key]['usages'].append({
                '作物名稱': row['作物名稱'],
                '病蟲害名稱': row['病蟲害名稱'],
                '病蟲害名稱_normalized': normalize_pest_name(row['病蟲害名稱']),
                '安全採收期': row['安全採收期'] or '',
                '稀釋倍數': row['稀釋倍數'] or '',
                '每公頃使用用藥量': row['每公頃使用用藥量'] or ''
            })
        for pesticide in pesticide_map.values():
            pesticide['usages'] = remove_duplicate_usages(pesticide['usages'])
        results.extend(pesticide_map.values())
    return results

#5.中文名稱的卡片
def handle_chem_only(cursor, chem_keywords):
    results = []
    for chem in chem_keywords:
        cursor.execute("SELECT * FROM crop_usage WHERE 中文名稱 = ?", (chem,))
        rows = cursor.fetchall()
        pesticide_map = {}
        for row in rows:
            key = f"{row['中文名稱']}__{row['劑型']}__{row['含量']}"
            if key not in pesticide_map:
                pesticide_map[key] = {
                    '中文名稱': f"{row['中文名稱']}",
                    '作物名稱': row['作物名稱'],
                    '病蟲害名稱': row['病蟲害名稱'],
                    '劑型': row['劑型'],
                    '含量': row['含量'],
                    '作用機制名稱': row['作用機制名稱'] or '',
                    '作用機制備註': row['作用機制備註'] or '',
                    'usages': []
                }
            pesticide_map[key]['usages'].append({
                '作物名稱': row['作物名稱'],
                '病蟲害名稱': row['病蟲害名稱'],
                '病蟲害名稱_normalized': normalize_pest_name(row['病蟲害名稱']),
                '安全採收期': row['安全採收期'] or '',
                '稀釋倍數': row['稀釋倍數'] or '',
                '每公頃使用用藥量': row['每公頃使用用藥量'] or ''
            })
        for pesticide in pesticide_map.values():
            pesticide['usages'] = remove_duplicate_usages(pesticide['usages'])
        results.extend(pesticide_map.values())
    return results

#6.廠牌名稱的卡片
def handle_brand_only(cursor, brand_keywords):
    results = []
    for brand in brand_keywords:
        cursor.execute("SELECT * FROM crop_usage WHERE 廠牌名稱 = ?", (brand,))
        rows = cursor.fetchall()
        pesticide_map = {}
        for row in rows:
            key = f"{row['中文名稱']}__{row['劑型']}__{row['含量']}"
            if key not in pesticide_map:
                # 標題顯示「中文名稱（廠牌名稱）」
                pesticide_map[key] = {
                    '中文名稱': f"{row['中文名稱']}（{row['廠牌名稱']}）",
                    '劑型': row['劑型'],
                    '含量': row['含量'],
                    '作用機制名稱': row['作用機制名稱'] or '',
                    '作用機制備註': row['作用機制備註'] or '',
                    'usages': []
                }
            pesticide_map[key]['usages'].append({
                '作物名稱': row['作物名稱'],
                '病蟲害名稱': row['病蟲害名稱'],
                '病蟲害名稱_normalized': normalize_pest_name(row['病蟲害名稱']),
                '安全採收期': row['安全採收期'] or '',
                '稀釋倍數': row['稀釋倍數'] or '',
                '每公頃使用用藥量': row['每公頃使用用藥量'] or ''
            })
        for pesticide in pesticide_map.values():
            pesticide['usages'] = remove_duplicate_usages(pesticide['usages'])
        results.extend(pesticide_map.values())
    return results

#7.條碼的卡片
def handle_barcode_only(cursor, barcode_keywords):
    results = []
    for barcode in barcode_keywords:
        cursor.execute("SELECT * FROM crop_usage WHERE 條碼 = ?", (barcode,))
        rows = cursor.fetchall()
        pesticide_map = {}
        for row in rows:
            key = f"{row['中文名稱']}__{row['劑型']}__{row['含量']}"
            if key not in pesticide_map:
                # 標題顯示「中文名稱（廠牌名稱） 條碼：xxxx」
                pesticide_map[key] = {
                    '中文名稱': f"{row['中文名稱']}（{row['廠牌名稱']}） 條碼：{row['條碼']}",
                    '劑型': row['劑型'],
                    '含量': row['含量'],
                    '作用機制名稱': row['作用機制名稱'] or '',
                    '作用機制備註': row['作用機制備註'] or '',
                    'usages': []
                }
            pesticide_map[key]['usages'].append({
                '作物名稱': row['作物名稱'],
                '病蟲害名稱': row['病蟲害名稱'],
                '病蟲害名稱_normalized': normalize_pest_name(row['病蟲害名稱']),
                '安全採收期': row['安全採收期'] or '',
                '稀釋倍數': row['稀釋倍數'] or '',
                '每公頃使用用藥量': row['每公頃使用用藥量'] or ''
            })
        for pesticide in pesticide_map.values():
            pesticide['usages'] = remove_duplicate_usages(pesticide['usages'])
        results.extend(pesticide_map.values())
    return results

#8.其他模糊查詢的卡片
def handle_fallback_partial_match(cursor, other_keywords):
    results = []
    for kw in other_keywords:
        cursor.execute(
            "SELECT * FROM crop_usage WHERE 作物名稱 LIKE ? OR 病蟲害名稱 LIKE ? OR 中文名稱 LIKE ? OR 廠牌名稱 LIKE ? OR 條碼 LIKE ?",
            tuple([f"%{kw}%"] * 5)
        )
        rows = cursor.fetchall()
        pesticide_map = {}
        for row in rows:
            key = f"{row['中文名稱']}__{row['劑型']}__{row['含量']}"
            if key not in pesticide_map:
                pesticide_map[key] = {
                    '中文名稱': row['中文名稱'],
                    '劑型': row['劑型'],
                    '含量': row['含量'],
                    '作用機制名稱': row['作用機制名稱'] or '',
                    '作用機制備註': row['作用機制備註'] or '',
                    'usages': []
                }
            pesticide_map[key]['usages'].append({
                '作物名稱': row['作物名稱'],
                '病蟲害名稱': row['病蟲害名稱'],
                '病蟲害名稱_normalized': normalize_pest_name(row['病蟲害名稱']),
                '安全採收期': row['安全採收期'] or '',
                '稀釋倍數': row['稀釋倍數'] or '',
                '每公頃使用用藥量': row['每公頃使用用藥量'] or ''
            })
        for pesticide in pesticide_map.values():
            pesticide['usages'] = remove_duplicate_usages(pesticide['usages'])
        results.extend(pesticide_map.values())
    return results

if __name__ == '__main__':
    app.run(debug=True)
