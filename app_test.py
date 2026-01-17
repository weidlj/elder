import streamlit as st
import websocket
import datetime
import hashlib
import base64
import hmac
import json
import os
from urllib.parse import urlencode
import time
import ssl
import asyncio
from io import BytesIO
import wave
from edge_tts import Communicate
from openai import OpenAI

CONFIG_FILE = "app_config.json"
DEFAULT_CONFIG = {
    "admin_password": "888",
    "xf_appid": "",
    "xf_api_key": "",
    "xf_api_secret": "",
    "deepseek_key": "",
    "contacts": {
        "儿子": "13800000001",
        "女儿": "13900000002"
    },
    "reminders": [
        {"time": "08:00", "task": "吃降压药"},
        {"time": "20:00", "task": "量血压"}
    ]
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

def validate_and_read_wav(file_bytes):
    try:
        with wave.open(BytesIO(file_bytes), 'rb') as wf:
            if wf.getframerate() != 16000 or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                return None, "仅支持 16kHz 单声道 PCM wav 文件"
            pcm_data = wf.readframes(wf.getnframes())
            return pcm_data, ""
    except Exception:
        return None, "音频文件解析失败，仅支持无压缩 wav"

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
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'), digestmod=hashlib.sha256).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')
        authorization_origin = "api_key=\"%s\", algorithm=\"%s\", headers=\"%s\", signature=\"%s\"" % (
            self.APIKey, "hmac-sha256", "host date request-line", signature_sha)
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        v = {"authorization": authorization, "date": date, "host": "ws-api.xfyun.cn"}
        return url + '?' + urlencode(v)

    def recognize_stream(self, audio_data):
        self.result_text = ""
        websocket.enableTrace(False)
        wsUrl = self.create_url()
        def on_message(ws, message):
            try:
                code = json.loads(message)["code"]
                if code == 0:
                    data = json.loads(message)["data"]["result"]["ws"]
                    result = ""
                    for i in data:
                        for w in i["cw"]:
                            result += w["w"]
                    self.result_text += result
            except:
                pass
        ws = websocket.WebSocketApp(wsUrl, on_message=on_message, on_error=print, on_close=lambda *a: None)
        ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        return self.result_text

async def edge_tts_generate(text, filename):
    communicate = Communicate(text, "zh-CN-YunyangNeural")
    await communicate.save(filename)

def generate_voice_file(text):
    filename = "reply_voice.mp3"
    asyncio.run(edge_tts_generate(text, filename))
    with open(filename, "rb") as f:
        return base64.b64encode(f.read()).decode()

def call_deepseek_intention(user_text, config):
    prompt = (
        "你是一个耐心贴心的老人助手。\n"
        "家属通讯录有：" + json.dumps(config['contacts'], ensure_ascii=False) +
        "。\n"
        "1. 要打电话，回复: CALL:联系人名。\n"
        "2. 身体不适，回复: ALERT:症状。\n"
        "3. 其他正常简短回答(30字内)。回复都只输出一行。"
    )
    try:
        client = OpenAI(api_key=config["deepseek_key"], base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"AI服务异常：{e}"

def handle_intent(ai_reply, config):
    action_call = None
    display_text = ai_reply
    if ai_reply.startswith("CALL:"):
        name = ai_reply.replace("CALL:","").strip()
        phone = config['contacts'].get(name)
        display_text = f"正在为您呼叫 {name}..."
        action_call = phone
    elif ai_reply.startswith("ALERT:"):
        content = ai_reply.replace("ALERT:","").strip()
        display_text = f"已通知家人：{content}"
    return display_text, action_call

st.set_page_config(page_title="关爱助手", page_icon="🧡", layout="centered", initial_sidebar_state="collapsed")

if 'config' not in st.session_state:
    st.session_state.config = load_config()
if 'page' not in st.session_state:
    st.session_state.page = "elder"
if 'last_file' not in st.session_state:
    st.session_state.last_file = None
if 'last_reply' not in st.session_state:
    st.session_state.last_reply = None
if 'audio_b64' not in st.session_state:
    st.session_state.audio_b64 = None
if 'action_call' not in st.session_state:
    st.session_state.action_call = None

def render_admin_page():
    st.markdown("## ⚙️ 家属配置后台")
    with st.form("admin_form"):
        with st.expander("🔐 API Key"):
            new_xf_appid = st.text_input("讯飞 APPID", value=st.session_state.config["xf_appid"])
            new_xf_key = st.text_input("讯飞 APIKey", value=st.session_state.config["xf_api_key"], type="password")
            new_xf_secret = st.text_input("讯飞 Secret", value=st.session_state.config["xf_api_secret"], type="password")
            new_ds_key = st.text_input("DeepSeek Key", value=st.session_state.config["deepseek_key"], type="password")
        with st.expander("📞 紧急联系人"):
            c_name1 = st.text_input("联系人1 称呼", "儿子")
            c_phone1 = st.text_input("联系人1 电话", st.session_state.config["contacts"].get("儿子", ""))
            c_name2 = st.text_input("联系人2 称呼", "女儿")
            c_phone2 = st.text_input("联系人2 电话", st.session_state.config["contacts"].get("女儿", ""))
        with st.expander("⏰ 每日提醒"):
            t_time = st.time_input("提醒时间", datetime.datetime.strptime(st.session_state.config["reminders"][0]["time"], "%H:%M").time())
            t_task = st.text_input("提醒内容", st.session_state.config["reminders"][0]["task"])
        if st.form_submit_button("💾 保存"):
            st.session_state.config.update({
                "xf_appid": new_xf_appid, "xf_api_key": new_xf_key, "xf_api_secret": new_xf_secret,
                "deepseek_key": new_ds_key,
                "contacts": {c_name1: c_phone1, c_name2: c_phone2},
                "reminders": [{"time": t_time.strftime("%H:%M"), "task": t_task}]
            })
            save_config(st.session_state.config)
            st.success("配置已更新")
            time.sleep(1)
            st.session_state.page = "elder"
            st.rerun()
    if st.button("⬅️ 返回"):
        st.session_state.page = "elder"
        st.rerun()

def render_auth_page():
    st.markdown("### 🔒 管理员验证")
    pwd = st.text_input("请输入密码 (默认888)", type="password")
    if st.button("进入"):
        if pwd == st.session_state.config["admin_password"]:
            st.session_state.page = "admin"
            st.rerun()
        else:
            st.error("密码错误")
    if st.button("取消"):
        st.session_state.page = "elder"
        st.rerun()

def render_elder_page():
    rem = st.session_state.config["reminders"][0]
    st.markdown(f"<div style='text-align:center; padding:15px; background:#E3F2FD; color:#1565C0; border-radius:10px; margin-bottom:20px;'>⏰ {rem['time']} 记得 {rem['task']}</div>", unsafe_allow_html=True)
    st.markdown("""
        <div style='display: flex; justify-content: center; align-items: center; height: 300px;'>
            <div style='width: 220px; height: 220px; border-radius: 50%; background: linear-gradient(145deg, #4CAF50, #45a049); box-shadow: 0 15px 35px rgba(76, 175, 80, 0.4); display: flex; flex-direction: column; justify-content: center; align-items: center; color: white; font-size: 26px; font-weight: bold; border: 8px solid #fff; text-align: center;'>
                🎙️<br>点击说话
            </div>
        </div>
    """, unsafe_allow_html=True)
    uploaded = st.file_uploader("上传 16kHz 单声道 PCM wav 文件", type=['wav'], label_visibility="collapsed")
    if uploaded:
        if not st.session_state.last_file or uploaded.name != st.session_state.last_file:
            st.session_state.last_file = uploaded.name
            file_bytes = uploaded.read()
            wav_data, error_msg = validate_and_read_wav(file_bytes)
            if error_msg:
                st.session_state.last_reply = error_msg
                return
            asr = XF_ASR(
                st.session_state.config["xf_appid"], 
                st.session_state.config["xf_api_key"], 
                st.session_state.config["xf_api_secret"]
            )
            with st.spinner("语音识别中..."):
                user_text = asr.recognize_stream(wav_data)
            if not user_text:
                st.session_state.last_reply = "没听清，请再说一次"
                return
            with st.spinner("AI理解中..."):
                ai_reply = call_deepseek_intention(user_text, st.session_state.config)
            display_text, action_call = handle_intent(ai_reply, st.session_state.config)
            st.session_state.last_reply = display_text
            st.session_state.action_call = action_call
            audio_b64 = generate_voice_file(display_text)
            st.session_state.audio_b64 = audio_b64
    if st.session_state.last_reply:
        st.markdown(f"<div style='background: #fff; padding: 20px; border-radius: 15px; margin: 15px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.1); font-size: 20px; border-left: 5px solid #4CAF50; text-align:center;'>{st.session_state.last_reply}</div>", unsafe_allow_html=True)
    if st.session_state.audio_b64:
        st.markdown(f"""
            <audio autoplay>
            <source src="data:audio/mp3;base64,{st.session_state.audio_b64}" type="audio/mp3">
            </audio>
        """, unsafe_allow_html=True)
    if st.session_state.action_call:
        num = st.session_state.action_call
        if num:
            st.markdown(f"""
                <a href="tel:{num}" style="display:block; width:100%; padding:20px; background:#FF5722; color:white; text-align:center; border-radius:10px; text-decoration:none; font-size:24px; font-weight:bold;">
                    📞 点击呼叫 ({num})
                </a>
            """, unsafe_allow_html=True)
    st.markdown("---")
    col1, col2 = st.columns([8, 1])
    with col2:
        if st.button("⚙️"):
            st.session_state.page = "auth"
            st.rerun()

if st.session_state.page == "elder":
    render_elder_page()
elif st.session_state.page == "auth":
    render_auth_page()
elif st.session_state.page == "admin":
    render_admin_page()