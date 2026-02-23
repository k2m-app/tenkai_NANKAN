import streamlit as st
import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
import time
import re
import traceback
import unicodedata

# ==========================================
# 0. ログイン＆セッション管理
# ==========================================

def login_keibabook(user_id, password):
    """ 競馬ブックにログインし、セッションを返す """
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    
    login_page_url = "https://s.keibabook.co.jp/login/login"
    
    try:
        # 1. ログインページにアクセスしてCSRFトークンを取得
        res = session.get(login_page_url)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        csrf_token = ""
        meta_csrf = soup.find('meta', {'name': 'csrf-token'})
        if meta_csrf:
            csrf_token = meta_csrf['content']
            
        # 2. ログイン情報をPOST
        payload = {
            '_token': csrf_token,
            'login_id': user_id, 
            'password': password
        }
        
        post_res = session.post(login_page_url, data=payload)
        
        # ログイン成功判定
        if "ログアウト" in post_res.text or "マイページ" in post_res.text:
            return session, True, "ログインに成功しました。"
        else:
            return session, False, "ログインに失敗しました。IDとパスワードを確認してください。"
            
    except Exception as e:
        return None, False, f"ログイン処理中にエラーが発生しました: {e}"

# ==========================================
# 1. ペース解析・展開予想のコアロジック (南関特化版)
# ==========================================

NANKAN_TRACK_BIAS = {
    "大井": 0.5,   
    "船橋": 0.0,   
    "川崎": -0.1,  
    "浦和": -0.3   
}

def calculate_early_pace_speed(row, current_dist):
    if pd.isna(row.get('early_3f')):
        return np.nan
        
    normalized_3f = row['early_3f']
    past_venue = row.get('venue', '')
    
    if past_venue in NANKAN_TRACK_BIAS:
        normalized_3f -= NANKAN_TRACK_BIAS[past_venue]
    elif past_venue not in ["東京", "中山", "京都", "阪神", "中京", "新潟", "福島", "小倉", "札幌", "函館"]:
        normalized_3f += 0.3 
    
    raw_speed = 600.0 / normalized_3f

    condition_mod = 0.0
    if row['track_condition'] in ["重", "不良"]: condition_mod = -0.15 
    elif row['track_condition'] == "稍": condition_mod = -0.05

    dist_diff = row['distance'] - current_dist
    distance_mod = 0.0
    if dist_diff > 0:
        distance_mod = -(dist_diff / 100.0) * 0.05
    elif dist_diff < 0:
        distance_mod = -(abs(dist_diff) / 100.0) * 0.10

    return raw_speed + condition_mod + distance_mod

def determine_running_style(past_df: pd.DataFrame) -> str:
    if past_df.empty: return "不明"
    is_good_run = (past_df['finish_position'] <= 4)
    good_runs = past_df[is_good_run]
    
    if good_runs.empty: return "不明"
    good_positions = good_runs['first_corner_pos'].tolist()
    
    if all(pos == 1 for pos in good_positions): return "ハナ絶対"
    if any(2 <= pos <= 5 for pos in good_positions): return "控えOK"
    return "差し追込"

def calculate_pace_score(horse, current_dist, current_venue, current_track, total_horses):
    past_df = pd.DataFrame(horse['past_races'])
    
    if past_df.empty: 
        horse['score'] = 10.0 + ((horse['horse_number'] - 1) * 0.05)
        horse['special_flag'] = "❓データ不足"
        horse['running_style'] = "不明"
        return horse['score']
    
    horse['running_style'] = determine_running_style(past_df)
    past_df['early_speed'] = past_df.apply(lambda row: calculate_early_pace_speed(row, current_dist), axis=1)
    max_speed = past_df['early_speed'].max()
    horse['max_early_speed'] = max_speed if not pd.isna(max_speed) else 16.0
    
    speed_advantage = 0.0
    if not pd.isna(max_speed):
        speed_advantage = (16.8 - max_speed) * 4.0 

    jockey_target = float(past_df.iloc[0]['first_corner_pos']) if not past_df.empty else 7.0
    base_position = (jockey_target * 0.6) + speed_advantage
    
    base_mod = (horse['horse_number'] - 1) * 0.05 
    horse['special_flag'] = ""
    late_start_penalty = 0.0
    
    if current_venue in ["浦和", "川崎"]:
        if horse['running_style'] == "差し追込":
            base_mod += 1.5
            horse['special_flag'] = "⚠️小回り差し厳重注意"
        
        if horse['horse_number'] <= 4:
            base_mod -= 0.5
        elif horse['horse_number'] >= 10:
            base_mod += 0.8
            horse['special_flag'] = (horse['special_flag'] + " 📉外枠不利").strip()
            
    elif current_venue == "大井":
        if horse['running_style'] == "差し追込":
            base_mod -= 0.5
            horse['special_flag'] = "✨大井差し警戒"

    last_race = past_df.iloc[0]
    weight_diff = horse['current_weight'] - last_race['weight']
    weight_modifier = weight_diff * 0.25
    
    is_outer_5 = horse['horse_number'] > (total_horses - 5)
    if is_outer_5 and weight_diff > -2.0 and horse['running_style'] != "ハナ絶対" and current_venue != "大井":
        late_start_penalty += 0.7 
        horse['special_flag'] = (horse['special_flag'] + " 👁️外枠様子見").strip()

    final_score = base_position + weight_modifier + base_mod + late_start_penalty
    return max(1.0, min(18.0, final_score))

def format_formation(sorted_horses):
    if not sorted_horses: return ""
    leaders, chasers, mid, backs = [], [], [], []
    top_score = sorted_horses[0]['score']
    for h in sorted_horses:
        num_str = chr(9311 + h['horse_number']) 
        score = h['score']
        if score <= top_score + 1.2 and len(leaders) < 3: leaders.append(num_str)
        elif score <= top_score + 4.5: chasers.append(num_str)
        elif score <= top_score + 9.5: mid.append(num_str)
        else: backs.append(num_str)
    
    parts = []
    if leaders: parts.append(f"({''.join(leaders)})")
    if chasers: parts.append("".join(chasers))
    if mid: parts.append("".join(mid))
    if backs: parts.append("".join(backs))
    return " ".join(parts)

# ==========================================
# 2. スクレイピングロジック（セッション対応版）
# ==========================================

def extract_corner_pos(text):
    text = text.strip()
    match = re.search(r'\d+', text)
    if match: return int(match.group())
    for char in text:
        try:
            if 'CIRCLED' in unicodedata.name(char): return int(unicodedata.numeric(char))
        except: pass
    return 7

def fetch_horse_details(session, horse_url, current_dist):
    try:
        response = session.get(horse_url)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        past_races = []
        
        history_divs = soup.select('div.uma_seiseki')
        for div in history_divs:
            if len(past_races) >= 5: break
                
            negahi_span = div.select_one('.negahi')
            if not negahi_span: continue
            date_venue = negahi_span.text.replace('\xa0', ' ').strip()
            parts = re.split(r'\s+', date_venue)
            p_venue = parts[-1] if len(parts) > 1 else "不明"
            
            kyori_span = div.select_one('.kyori')
            dist = current_dist
            baba_cond = "良"
            if kyori_span:
                k_text = kyori_span.text
                d_match = re.search(r'\d+', k_text)
                if d_match: dist = int(d_match.group())
                if "不良" in k_text: baba_cond = "不良"
                elif "重" in k_text: baba_cond = "重"
                elif "稍" in k_text: baba_cond = "稍"
                
            finish_pos = 5
            cyakujun_span = div.select_one('.cyakujun')
            if cyakujun_span:
                f_match = re.search(r'\d+', cyakujun_span.text)
                if f_match: finish_pos = int(f_match.group())
                
            early_3f = np.nan
            agari_span = div.select_one('.agari')
            if agari_span:
                agari_text = agari_span.text.strip()
                matches = re.findall(r'(\d+\.\d+)', agari_text)
                if matches: early_3f = float(matches[0]) 
                    
            first_corner = 7
            tuka_lis = div.select('.tuka li span')
            if tuka_lis: first_corner = extract_corner_pos(tuka_lis[0].text)
                
            weight = 480.0
            batai_span = div.select_one('.batai')
            if batai_span:
                w_match = re.search(r'(\d+)', batai_span.text)
                if w_match: weight = float(w_match.group())
                
            past_races.append({
                'venue': p_venue, 'track_type': "ダート", 'distance': dist,
                'track_condition': baba_cond, 'finish_position': finish_pos,
                'popularity': 5, 'early_3f': early_3f,
                'first_corner_pos': first_corner, 'is_late_start': False,
                'past_frame': 4, 'weight': weight
            })
            
        return past_races
    except Exception as e:
        return []

@st.cache_data(ttl=600, show_spinner=False)
def fetch_real_data(_session, target_race_id: str):
    race_url = f"https://s.keibabook.co.jp/chihou/syutuba/{target_race_id}"
    try:
        response = _session.get(race_url)
        response.encoding = 'utf-8' 
        soup = BeautifulSoup(response.text, 'html.parser')
        
        racemei_elem = soup.select_one('.racemei p')
        current_venue = racemei_elem.text.strip() if racemei_elem else "不明"
        if current_venue == "不明": return None, 1400, "", "ダート", "出馬表データが見つかりません。"
        
        sub_info = soup.select('.racetitle_sub p')
        dist_text = ""
        for p in sub_info:
            if "m" in p.text:
                dist_text = p.text
                break
                
        current_dist = int(re.search(r'\d+', dist_text).group()) if dist_text else 1400
        current_track = "ダート"

        horses_data = []
        rows = soup.select('table.syutuba_sp tbody tr')
        if not rows: return None, current_dist, current_venue, current_track, "出走馬データが見つかりません。"

        progress_bar = st.progress(0)
        total_rows = len(rows)

        for i, row in enumerate(rows):
            umaban_td = row.select_one('td[class^="waku"]')
            if not umaban_td: continue
            horse_num = int(umaban_td.text.strip())
            
            bamei_a = row.select_one('.kbamei a')
            if not bamei_a: continue
            horse_name = bamei_a.text.strip()
            horse_url = "https://s.keibabook.co.jp" + bamei_a['href']
            
            past_races = fetch_horse_details(_session, horse_url, current_dist)
            current_weight = past_races[0]['weight'] if past_races else 480.0
            
            horses_data.append({
                'horse_number': horse_num, 'horse_name': horse_name,
                'current_weight': current_weight, 'past_races': past_races,
                'score': 0.0, 'special_flag': ""
            })
            
            time.sleep(0.5) 
            progress_bar.progress((i + 1) / total_rows)
            
        progress_bar.empty()
        if not horses_data: return None, 1400, "", "ダート", "馬データが取得できませんでした。"
        return horses_data, current_dist, current_venue, current_track, None
        
    except Exception as e:
        return None, 1400, "", "ダート", f"エラー: {e}"

# ==========================================
# 3. スマホ対応UI & Secretsログイン処理
# ==========================================
st.set_page_config(page_title="AI南関展開予想", page_icon="🏇", layout="centered")

# セッションステートの初期化
if 'kb_session' not in st.session_state:
    st.session_state.kb_session = requests.Session()
if 'is_logged_in' not in st.session_state:
    st.session_state.is_logged_in = False

# Secretsから情報を取得（存在しない場合のエラーハンドリング）
try:
    secret_id = st.secrets["keibabook"]["login_id"]
    secret_pw = st.secrets["keibabook"]["password"]
    has_secrets = True
except (KeyError, FileNotFoundError):
    has_secrets = False

with st.sidebar:
    st.header("🔑 競馬ブック ログイン")
    
    if not st.session_state.is_logged_in:
        if has_secrets:
            # Secretsに情報があればボタン一つでログイン
            st.info("Secretsに認証情報が設定されています。")
            if st.button("🔒 自動ログイン実行", type="primary"):
                with st.spinner("ログイン中..."):
                    session, success, msg = login_keibabook(secret_id, secret_pw)
                    if success:
                        st.session_state.kb_session = session
                        st.session_state.is_logged_in = True
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        else:
            # Secretsがない場合は手動入力を促すフォールバック
            st.warning("Secretsが設定されていません。手動で入力してください。")
            kb_id = st.text_input("ログインID (メールアドレス等)")
            kb_pw = st.text_input("パスワード", type="password")
            if st.button("ログイン実行"):
                with st.spinner("ログイン中..."):
                    session, success, msg = login_keibabook(kb_id, kb_pw)
                    if success:
                        st.session_state.kb_session = session
                        st.session_state.is_logged_in = True
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
    else:
        st.success("ログイン済みです ✅")
        if st.button("ログアウト"):
            st.session_state.kb_session = requests.Session()
            st.session_state.is_logged_in = False
            st.rerun()

st.title("🏇 AI競馬展開予想 (南関特化版)")
st.markdown("大井の白砂補正や、浦和・川崎の強い前残りバイアスを加味した隊列予想を行います。※プレミアムデータを取得するため、サイドバーからのログインが必要です。")

with st.container(border=True):
    st.subheader("⚙️ レース設定")
    base_url_input = st.text_input("🔗 地方競馬出馬表URLを貼り付け", value="https://s.keibabook.co.jp/chihou/syutuba/2026021301010223")
    
    try:
        selected_races = st.pills("レース番号", options=list(range(1, 13)), default=[1], format_func=lambda x: f"{x}R", selection_mode="multi")
    except AttributeError:
        selected_races = st.multiselect("レース番号", options=list(range(1, 13)), default=[1], format_func=lambda x: f"{x}R")

    if not isinstance(selected_races, list):
        selected_races = [selected_races] if selected_races else []

    col1, col2 = st.columns(2)
    with col1:
        execute_btn = st.button("🚀 選択レースを予想", type="primary", use_container_width=True, disabled=not st.session_state.is_logged_in)
    with col2:
        execute_all_btn = st.button("🌟 全12Rを一括予想", type="secondary", use_container_width=True, disabled=not st.session_state.is_logged_in)
        
    if not st.session_state.is_logged_in:
        st.error("⚠️ 左のサイドバーから競馬ブックにログインしてください。")

run_inference = False
target_races = []
url_prefix = ""
url_suffix = ""

match = re.search(r'(\d{10})(\d{2})(\d{4})', base_url_input)
if match:
    url_prefix = match.group(1)
    url_suffix = match.group(3)

if execute_all_btn:
    run_inference = True
    target_races = list(range(1, 13))
elif execute_btn:
    if not selected_races:
        st.warning("レース番号を選択してください。")
    else:
        run_inference = True
        target_races = selected_races

if run_inference:
    if not match:
        st.error("有効な競馬ブック地方競馬のレースURLが見つかりません。")
    else:
        for race_num in sorted(target_races):
            target_race_id = f"{url_prefix}{race_num:02d}{url_suffix}"
            st.markdown(f"### 🏁 {race_num}R")
            
            with st.spinner(f"{race_num}R の各馬の詳細データを解析中..."):
                horses, current_dist, current_venue, current_track, error_msg = fetch_real_data(st.session_state.kb_session, target_race_id)
                
                if error_msg:
                    st.warning(f"{error_msg}")
                    continue
                    
                total_horses = len(horses)
                for horse in horses:
                    horse['score'] = calculate_pace_score(horse, current_dist, current_venue, current_track, total_horses)
                    
                sorted_horses = sorted(horses, key=lambda x: x['score'])
                formation_text = format_formation(sorted_horses)

            st.info(f"📏 条件: **{current_venue} {current_track}{current_dist}m** ({total_horses}頭立て)")
            st.markdown(f"<h4 style='text-align: center; letter-spacing: 2px;'>◀(進行方向)</h4>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center; color: #FF4B4B;'>{formation_text}</h3>", unsafe_allow_html=True)
            st.markdown("---")
            
            with st.expander(f"📊 {race_num}R の詳細データを見る"):
                df_result = pd.DataFrame([{
                    "馬番": h['horse_number'],
                    "馬名": h['horse_name'],
                    "スコア": round(h['score'], 2),
                    "戦法": h.get('running_style', ''),
                    "特記事項": h.get('special_flag', '')
                } for h in sorted_horses])
                st.dataframe(df_result, use_container_width=True, hide_index=True)
            st.markdown("<br>", unsafe_allow_html=True)
