import re
import os
import aiofiles
import requests
import schedule
import time
from nonebot import on_message, on_command
from nonebot.adapters.onebot.v11 import (
    Bot,
    MessageEvent,
    GroupMessageEvent,
    MessageSegment,
)
from openai import OpenAI
from sparkai.llm.llm import ChatSparkLLM, ChunkPrintHandler
from sparkai.core.messages import ChatMessage
from .config import deepseek_key, SPARKAI_APP_ID, SPARKAI_API_SECRET, SPARKAI_API_KEY
from .word2picture import generate_image, save_image

# 初始化 OpenAI 客户端
api_key = deepseek_key
client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

# 初始化 SparkAI 客户端
SPARKAI_URL = 'wss://spark-api.xf-yun.com/v3.5/chat'
SPARKAI_DOMAIN = 'generalv3.5'
spark = ChatSparkLLM(
    spark_api_url=SPARKAI_URL,
    spark_app_id=SPARKAI_APP_ID,
    spark_api_key=SPARKAI_API_KEY,
    spark_api_secret=SPARKAI_API_SECRET,
    spark_llm_domain=SPARKAI_DOMAIN,
    streaming=False,
)

# 全局存储
user_data = {}

# 消息处理器
message_handler = on_message(priority=10)
model_command = on_command("model", aliases={"模型"}, priority=1)
clear_command = on_command("clear", aliases={"清理"}, priority=1)
balance_command = on_command("balance", aliases={"余额"}, priority=1)
img_command = on_command("img", aliases={"图片"}, priority=1)
help_command = on_command("help", aliases={"帮助"}, priority=1)

async def split_response(text):
    """将响应拆分为前代码、代码块和后代码部分"""
    code_blocks = re.finditer(r'```(.*?)\n(.*?)```', text, re.DOTALL)
    parts = []
    last_end = 0
    code_parts = []
    
    for match in code_blocks:
        # 添加代码块前的文本
        if match.start() > last_end:
            parts.append(("text", text[last_end:match.start()].strip()))
        
        # 添加代码块
        lang, code = match.groups()
        code_parts.append((lang, code.strip()))
        parts.append(("code", (lang, code.strip())))
        last_end = match.end()
    
    # 添加剩余文本
    if last_end < len(text):
        parts.append(("text", text[last_end:].strip()))
    
    return parts, code_parts

async def upload_file(bot, event, user_id, filename):
    """将文件上传到相应的聊天"""
    abs_filename = os.path.abspath(filename)
    if isinstance(event, GroupMessageEvent):
        await bot.call_api(
            'upload_group_file',
            group_id=event.group_id,
            file=abs_filename,
            name=os.path.basename(abs_filename)
        )
    else:
        await bot.call_api(
            'upload_private_file',
            user_id=int(user_id),
            file=abs_filename,
            name=os.path.basename(abs_filename)
        )

async def handle_deepseek(history):
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=history,
        stream=False
    )
    return response.choices[0].message.content

async def handle_spark(history):
    messages = [ChatMessage(role=msg["role"], content=msg["content"]) for msg in history]
    response = spark.generate([messages])
    return response.generations[0][0].message.content

async def process_ai_response(user_model, history):
    """根据用户模型处理 AI 响应"""
    if user_model == "deepseek-chat" or user_model == "deepseek-coder":
        return await handle_deepseek(history)
    else:
        return await handle_spark(history)

async def process_message(bot, event, user_id, user_input):
    """处理用户消息并获取 AI 响应"""
    data = user_data.get(user_id, {"history": [], "model": "deepseek-chat"})
    history = data["history"]
    user_model = data["model"]

    history.append({"role": "user", "content": user_input})
    assistant_reply = await process_ai_response(user_model, history)
    history.append({"role": "assistant", "content": assistant_reply})
    user_data[user_id] = {"history": history, "model": user_model}

    return assistant_reply, history

async def send_response(bot, event, user_id, assistant_reply):
    """发送 AI 响应给用户"""
    parts, code_parts = await split_response(assistant_reply)
    
    if code_parts:
        try:
            # 将代码块保存到 txt 文件
            current_dir = os.path.dirname(os.path.abspath(__file__))
            for lang, code in code_parts:
                filename = os.path.join(current_dir, f'code_{user_id}_{lang}.txt')
                async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
                    await f.write(code)

                # 按顺序发送消息
                for part_type, content in parts:
                    if part_type == "text" and content:
                        if isinstance(event, GroupMessageEvent):
                            await bot.send(event, MessageSegment.at(user_id) + MessageSegment.text(" ") + content)
                        else:
                            await bot.send(event, content)
                    elif part_type == "code":
                        # 发送 txt 文件
                        await upload_file(bot, event, user_id, filename)

        except Exception as e:
            await bot.send(event, f"文件发送失败: {e}")
        finally:
            # 清理
            for lang, _ in code_parts:
                filename = os.path.join(current_dir, f'code_{user_id}_{lang}.txt')
                if os.path.exists(filename):
                    os.remove(filename)
    else:
        # 发送普通消息
        if isinstance(event, GroupMessageEvent):
            await bot.send(event, MessageSegment.at(user_id) + MessageSegment.text(" ") + assistant_reply)
        else:
            await bot.send(event, assistant_reply)

@message_handler.handle()
async def handle_message(bot: Bot, event: MessageEvent):
    user_id = event.get_user_id()
    user_input = event.get_plaintext().strip()

    # 跳过空消息
    if not user_input:
        return

    # 跳过命令
    if event.raw_message.startswith(("/", "!", ".")):
        return

    # 处理群消息
    if isinstance(event, GroupMessageEvent):
        at_me = f"[CQ:at,qq={bot.self_id}]" in event.raw_message
        if not at_me:
            print(f"未检测到@机器人: {event.raw_message}")
            return

    print(f"检测到@机器人: {event.raw_message}")

    try:
        assistant_reply, history = await process_message(bot, event, user_id, user_input)
        await send_response(bot, event, user_id, assistant_reply)
    except Exception as e:
        await bot.send(event, f"处理消息时出错: {e}")

@model_command.handle()
async def handle_model_command(event: MessageEvent):
    user_id = event.get_user_id()
    data = user_data.get(user_id, {"history": [], "model": "deepseek-chat"})
    current_model = data["model"]
    
    if current_model == "deepseek-chat":
        new_model = "deepseek-coder"
    elif current_model == "deepseek-coder":
        new_model = "spark"
    else:
        new_model = "deepseek-chat"

    data["model"] = new_model
    data["history"] = []  # 切换模型时清理历史记录
    user_data[user_id] = data
    
    await model_command.finish(f"模型已切换为 {new_model}")

@clear_command.handle()
async def handle_clear_command(event: MessageEvent):
    user_id = event.get_user_id()
    if user_id in user_data:
        user_data[user_id]["history"] = []
    await clear_command.finish("历史记录已清理")

@balance_command.handle()
async def handle_balance_command(event: MessageEvent):
    url = "https://api.deepseek.com/user/balance"
    payload={}
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }

    try:
        response = requests.request("GET", url, headers=headers, data=payload)
        response.raise_for_status()
        balance_info = response.json()
        
        # 提取余额信息
        balance_infos = balance_info.get("balance_infos", [])
        if balance_infos:
            balance = balance_infos[0].get("total_balance", "未知")
            currency = balance_infos[0].get("currency", "未知")
            await balance_command.finish(f"当前余额: {balance} {currency}")
        else:
            await balance_command.finish("未能获取余额信息")

    except requests.RequestException as e:
        await balance_command.finish(f"查询余额时出错: {e}")

@img_command.handle()
async def handle_img_command(bot: Bot, event: MessageEvent):
    user_id = event.get_user_id()
    user_input = event.get_plaintext().strip()

    # 跳过空消息
    if not user_input:
        return

    # 从用户输入中提取描述
    description = user_input[len("/img "):].strip()

    try:
        # 生成图片
        response = await generate_image(
            description, 
            SPARKAI_APP_ID, 
            SPARKAI_API_KEY, 
            SPARKAI_API_SECRET
        )
        
        # 保存图片
        image_path = await save_image(response)

        # 确保目录存在
        pic_dir = os.path.dirname(image_path)
        if not os.path.exists(pic_dir):
            os.makedirs(pic_dir)

        print(f"Image path: {image_path}")
        print(f"File exists: {os.path.exists(image_path)}")

        # 发送图片
        if os.path.exists(image_path):
            await upload_file(bot, event, user_id, image_path)
        else:
            await img_command.finish("图片生成失败")

    except Exception as e:
        await img_command.finish(f"处理消息时出错: {e}")
        raise e

@help_command.handle()
async def handle_help_command(bot: Bot, event: MessageEvent):
    help_message = (
        "可用指令：\n"
        "/help(或帮助) - 显示此帮助信息\n"
        "/model(或模型) - 切换模型\n"
        "/clear(或清理） - 清理历史记录\n"
        "/balance(或余额) - 查询余额\n"
        "/img(或图片) [文字描述] - 根据描述生成图片\n"
        "/music(点歌) [歌名] - 网易云点歌\n"
    )
    await help_command.finish(help_message)

# 定时任务：每天删除图片和历史记录
def delete_files_and_clear_history():
    # 删除图片
    pic_dir = os.path.abspath("./pic")
    if os.path.exists(pic_dir):
        for filename in os.listdir(pic_dir):
            file_path = os.path.join(pic_dir, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
    
    # 清理历史记录
    global user_data
    user_data = {}

# 安排定时任务
schedule.every().day.at("00:00").do(delete_files_and_clear_history)

# 启动定时任务
def run_schedule():
    while True:
        schedule.run_pending()
        time.sleep(1)

# 启动定时任务线程
import threading
threading.Thread(target=run_schedule, daemon=True).start()