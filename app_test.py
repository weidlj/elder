import streamlit as st
import websocket
import datetime
import hashlib
import base64
import hmac
import json
from urllib.parse import urlencode
import time
import ssl
import threading
import _thread as thread
import os
import asyncio
import edge_tts
from openai import OpenAI
from io import BytesIO
from pydub import AudioSegment

# ==========================================
# 0. 基���配置与文件持久化
# ==========================================
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "xf_appid": "",
    "xf_apikey": "",
    "xf_secret": "",
    "ds_key": "",
    "admin_password": "888",
    "contacts": {"儿子": "13800000001", "女儿": "13900000002"},
    "reminders": [{"time": "08:00", "task": "吃降压药"}, {"time": "20:00", "task": "量血压"}]
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding='utf-8') as f:
                return json.load(f)
        except:
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config(config):
    with open(CONFIG_FILE, "w", encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

# ==========================================
# 1. 科大讯飞语音识别类
# ==========================================
class XF_ASR(object):
    def __init__(self, APPID, APIKey, APISecret):
        self.APPID = APPID
        self.APIKey = APIKey
        self.APISecret = APISecret
        self.result_text = ""

    def create_url(self):
        url = 'wss://iat-api.xfyun.cn/v2/iat'
        now = datetime.datetime.now()
        date = now.strftime('%a, %d %b %Y %H:%M:%S GMT')
        signature_origin = "host: " + "ws-api.xfyun.cn" + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + "/v2/iat" + " HTTP/1.1"
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'),
                                 digestmod=hashlib.sha256).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')
        authorization_origin = "api_key=\"%s\", algorithm=\"%s\", headers=\"%s\", signature=\"%s\"" % (
            self.APIKey, "hmac-sha256", "host date request-line", signature_sha)
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        v = { "authorization": authorization, "date": date, "host": "ws-api.xfyun.cn" }
        return url + '?' + urlencode(v)

    def recognize_stream(self, audio_data):
        self.result_text = ""
        wsParam = self
        websocket.enableTrace(False)
        wsUrl = self.create_url()
        
        def on_message(ws, message):
            try:
                code = json.loads(message)["code"]
                if code != 0:
                    print(f"Error: {json.loads(message)['message']}")
                else:
                    data = json.loads(message)["data"]["result"]["ws"]
                    result = ""
                    for i in data:
                        for w in i["cw"]:
                            result += w["w"]
                    self.result_text += result
            except Exception as e:
                print("Parse exception:", e)

        def on_error(ws, error):
            print("### error:", error)

        def on_close(ws, a, b):
            pass

        def on_open(ws):
            def run(*args):
                frameSize = 8000
                intervel = 0.04
                status = 0
                offset = 0
                while offset < len(audio_data):
                    buf = audio_data[offset:offset+frameSize]
                    offset += frameSize
                    if offset >= len(audio_data):
                        status = 2
                    if status == 0:
                        d = {"common": {"app_id": wsParam.APPID},
                             "business": {"domain": "iat", "language": "zh_cn", "accent": "mandarin", "vcn": "xiaoyan"},
                             "data": {"status": 0, "format": "audio/L16;rate=16000",
                                      "audio": str(base64.b64encode(buf), 'utf-8'), "encoding": "raw"}}
                        ws.send(json.dumps(d))
                        status = 1
                    elif status == 1:
                        d = {"data": {"status": 1, "format": "audio/L16;rate=16000",
                                      "audio": str(base64.b64encode(buf), 'utf-8'), "encoding": "raw"}}
                        ws.send(json.dumps(d))
                    elif status == 2:
                        d = {"data": {"status": 2, "format": "audio/L16;rate=16000",
                                      "audio": str(base64.b64encode(buf), 'utf-8'), "encoding": "raw"}}
                        ws.send(json.dumps(d))
                        time.sleep(1)
                        break
                    time.sleep(intervel)
                ws.close()
            thread.start_new_thread(run, ())

        ws = websocket.WebSocketApp(wsUrl, on_message=on_message, on_error=on_error, on_close=on_close)
        ws.on_open = on_open
        ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        return self.result_text

# ==========================================
# 2. EdgeTTS 语音合成
# ==========================================
async def edge_tts_generate(text, filename):
    communicate = edge_tts.Communicate(text, "zh-CN-YunyangNeural")
    await communicate.save(filename)

def generate_voice_file(text):
    filename = "temp_reply.mp3"
    try:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        if loop.is_running():
            asyncio.create_task(edge_tts_generate(text, filename))
        else:
            loop.run_until_complete(edge_tts_generate(text, filename))
        return filename
    except Exception as e:
        asyncio.run(edge_tts_generate(text, filename))
        return filename

# ==========================================
# 3. 业务逻辑处理 (含 FFmpeg 转码)
# ==========================================
def process_pipeline(uploaded_file, config):
    # [Step 1] FFmpeg 转码
    try:
        audio_bytes = uploaded_file.read()
        audio = AudioSegment.from_file(BytesIO(audio_bytes))
        # 强制转为 16000Hz, 单声道, 16bit
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        wav_buffer = BytesIO()
        audio.export(wav_buffer, format="wav")
        wav_data = wav_buffer.getvalue()
    except Exception as e:
        return f"转码失败: {e} (请检查ffmpeg是否安装)", None

    # [Step 2] 讯飞识别
    if not (config["xf_appid"] and config["xf_apikey"] and config["xf_secret"]):
        return "请家属先配置 API Key", None
        
    asr = XF_ASR(config["xf_appid"], config["xf_apikey"], config["xf_secret"])
    user_text = asr.recognize_stream(wav_data)
    
    if not user_text:
        return "没听清，请再说一次", None

    # [Step 3] DeepSeek 思考
    if not config["ds_key"]:
        ai_reply = f"听到: {user_text} (未配置DeepSeek)"
    else:
        try:
            client = OpenAI(api_key=config["ds_key"], base_url="https://api.deepseek.com")
            prompt = f"你是一个老人助手。通讯录：{json.dumps(config['contacts'], ensure_ascii=False)}。简短回答。如需打电话回复 CALL:名字。"
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_text}]
            )
            ai_reply = resp.choices[0].message.content
        except Exception as e:
            ai_reply = f"AI Error: {e}"

    # [Step 4] 语音合成
    display_text = ai_reply
    if ai_reply.startswith("CALL:"):
        name = ai_reply.split(":")[1]
        num = config["contacts"].get(name)
        display_text = f"正在呼叫{name}..."
        st.session_state['call_num'] = num
        
    audio_file = generate_voice_file(display_text)
    return display_text, audio_file

# ==========================================
# 4. 界面渲染
# ==========================================

# 初始化设置 (Centered 布局)
st.set_page_config(page_title="智能伴侣", page_icon="🧡", layout="centered", initial_sidebar_state="collapsed")

# 注入 CSS：极简风，隐藏侧边栏，大按钮
st.markdown("""
    <style>
    /* 隐藏顶部和汉堡菜单 */
    header, footer, [data-testid="stSidebar"] {display: none;}
    
    /* 老人模式大按钮容器 */
    .big-btn-container {
        display: flex; justify-content: center; align-items: center;
        height: 300px; margin-top: 20px; position: relative;
    }
    .circle-btn {
        width: 220px; height: 220px; border-radius: 50%;
        background: linear-gradient(145deg, #4CAF50, #45a049);
        box-shadow: 0 15px 35px rgba(76, 175, 80, 0.4);
        display: flex; flex-direction: column; justify-content: center; align-items: center;
        color: white; font-size: 26px; font-weight: bold; border: 8px solid #fff;
        text-align: center;
    }
    /* 覆盖在上面的透明上传组件 */
    [data-testid='stFileUploader'] {
        position: absolute; width: 220px; height: 220px; opacity: 0; cursor: pointer; z-index: 99;
    }
    
    /* 提醒和对话框 */
    .reminder-box {
        background-color: #E3F2FD; color: #1565C0; padding: 15px;
        border-radius: 10px; text-align: center; margin-bottom: 20px; font-weight: bold;
    }
    .chat-card {
        background: #fff; padding: 20px; border-radius: 15px; 
        box-shadow: 0 4px 15px rgba(0,0,0,0.05); text-align: center;
        font-size: 22px; margin-top: 20px; line-height: 1.6;
    }
    
    /* 设置入口按钮 */
    .settings-trigger {
        position: fixed; bottom: 10px; right: 10px; 
        opacity: 0.2; font-size: 20px; cursor: pointer;
    }
    </style>
""", unsafe_allow_html=True)

# 状态初始化
if 'config' not in st.session_state:
    st.session_state.config = load_config()
if 'mode' not in st.session_state:
    st.session_state.mode = 'elder' # elder / admin_login / admin_panel

# -----------------------------------
# 路由逻辑
# -----------------------------------

# === 场景 1: 家属登录验证 ===
if st.session_state.mode == 'admin_login':
    st.markdown("### 🔐 家属设置后台")
    pwd = st.text_input("输入管理密码", type="password")
    c1, c2 = st.columns(2)
    if c1.button("确认"):
        if pwd == st.session_state.config["admin_password"]:
            st.session_state.mode = 'admin_panel'
            st.rerun()
        else:
            st.error("密码错误")
    if c2.button("返回老人模式"):
        st.session_state.mode = 'elder'
        st.rerun()

# === 场景 2: 家属配置面板 ===
elif st.session_state.mode == 'admin_panel':
    st.markdown("### ⚙️ 配置中心")
    
    with st.form("settings_form"):
        st.subheader("1. API Key 配置")
        new_xf_app = st.text_input("讯飞 APPID", st.session_state.config["xf_appid"])
        new_xf_key = st.text_input("讯飞 APIKey", st.session_state.config["xf_apikey"])
        new_xf_sec = st.text_input("讯飞 Secret", st.session_state.config["xf_secret"])
        new_ds_key = st.text_input("DeepSeek Key", st.session_state.config["ds_key"])
        
        st.subheader("2. 紧急联系人")
        # 简单演示：只编辑第一个联系人
        c_name = st.text_input("称呼 (如: 儿子)", "儿子")
        c_num = st.text_input("电话号码", st.session_state.config["contacts"].get("儿子", ""))
        
        st.subheader("3. 闹钟提醒")
        r_time = st.text_input("提醒时间 (HH:MM)", st.session_state.config["reminders"][0]["time"])
        r_task = st.text_input("提醒内容", st.session_state.config["reminders"][0]["task"])
        
        if st.form_submit_button("💾 保存配置"):
            # 更新 Config
            cfg = st.session_state.config
            cfg["xf_appid"] = new_xf_app
            cfg["xf_apikey"] = new_xf_key
            cfg["xf_secret"] = new_xf_sec
            cfg["ds_key"] = new_ds_key
            cfg["contacts"][c_name] = c_num
            cfg["reminders"][0] = {"time": r_time, "task": r_task}
            
            save_config(cfg)
            st.success("保存成功！")
            time.sleep(1)
            st.session_state.mode = 'elder'
            st.rerun()

    if st.button("取消并返回"):
        st.session_state.mode = 'elder'
        st.rerun()

# === 场景 3: 老人主界面 (Zen Mode) ===
else:
    # 1. 顶部提醒
    rem = st.session_state.config["reminders"][0]
    st.markdown(f"<div class='reminder-box'>📅 温馨提醒：{rem['time']} 记得 {rem['task']}</div>", unsafe_allow_html=True)
    
    st.markdown("<h2 style='text-align:center;'>👵 智能伴侣</h2>", unsafe_allow_html=True)

    # 2. 巨大的交互按钮 (利用 file_uploader 覆盖)
    st.markdown("""
        <div class="big-btn-container">
            <div class="circle-btn">
                🎙️<br>点击说话
            </div>
        </div>
    """, unsafe_allow_html=True)
    
    # 核心上传���件
    uploaded = st.file_uploader(" ", type=['wav', 'mp3', 'm4a', 'aac', 'ogg'], label_visibility="collapsed")
    
    # 结果展示占位符
    res_box = st.empty()
    
    # 3. 处理逻辑
    if uploaded:
        if 'last_file' not in st.session_state or st.session_state.last_file != uploaded.name:
            st.session_state.last_file = uploaded.name
            
            with st.spinner("⏳ 正在听懂您说的话..."):
                # 调用处理管道
                reply_txt, reply_audio = process_pipeline(uploaded, st.session_state.config)
            
            # 显示文字
            res_box.markdown(f"<div class='chat-card'>🤖 {reply_txt}</div>", unsafe_allow_html=True)
            
            # 播放语音
            if reply_audio:
                with open(reply_audio, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                st.markdown(f"""
                    <audio autoplay style="display:none;">
                        <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
                    </audio>
                """, unsafe_allow_html=True)
                
            # 拨打电话
            if 'call_num' in st.session_state:
                num = st.session_state.pop('call_num')
                if num:
                    st.markdown(f"""
                        <a href="tel:{num}" style="display:block; margin:20px; padding:20px; background:#4CAF50; color:white; text-align:center; border-radius:15px; text-decoration:none; font-size:24px;">
                        📞 点击立即呼叫 {num}
                        </a>
                    """, unsafe_allow_html=True)

    # 4. 隐蔽的后台入口 (页面右下角)
    st.markdown("---")
    col1, col2 = st.columns([8, 1])
    with col2:
        if st.button("⚙️"):
            st.session_state.mode = 'admin_login'
            st.rerun()
