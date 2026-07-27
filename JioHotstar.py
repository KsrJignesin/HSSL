import logging
from io import BytesIO
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
import json
import os
import base64
import glob
import re
import random
import cloudscraper
import zipfile
import shutil
from datetime import datetime

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ADMIN_ID = 6177293322 #Change it to yours

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

STATS_FILE = "bot_stats.json"

def load_stats():
    try:
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {
            'total_activations': 0,
            'successful_activations': 0,
            'failed_activations': 0,
            'cookies_used': 0,
            'last_activation': None
        }

def save_stats(stats):
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=2)

def update_stats(success=True):
    stats = load_stats()
    stats['total_activations'] += 1
    if success:
        stats['successful_activations'] += 1
    else:
        stats['failed_activations'] += 1
    stats['last_activation'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_stats(stats)
    return stats

COOKIE_USAGE_FILE = "cookie_usage.json"

def load_cookie_usage():
    try:
        with open(COOKIE_USAGE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_cookie_usage(usage):
    with open(COOKIE_USAGE_FILE, 'w') as f:
        json.dump(usage, f, indent=2)

def get_cookie_usage_count(cookie_name):
    usage = load_cookie_usage()
    return usage.get(cookie_name, 0)

def increment_cookie_usage(cookie_name):
    usage = load_cookie_usage()
    usage[cookie_name] = usage.get(cookie_name, 0) + 1
    save_cookie_usage(usage)
    return usage[cookie_name]

def parse_cookie_file(filepath):
    cookies = {}
    session_token = None
    user_up_token = None
    device_id = None
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    name = parts[5]
                    value = parts[6]
                    cookies[name] = value
                    if name == 'sessionUserUP':
                        session_token = value
                    elif name == 'userUP':
                        user_up_token = value
                    elif name == 'deviceId':
                        device_id = value
    except:
        pass
    
    cookie_str = '; '.join([f"{k}={v}" for k, v in cookies.items()])
    return cookie_str, session_token, user_up_token, device_id

def get_random_cookie():
    cookie_files = glob.glob("cookies/*.txt")
    
    if not cookie_files:
        return None, "No cookie files found in vault"

    available_cookies = []
    for filepath in cookie_files:
        filename = os.path.basename(filepath)
        usage_count = get_cookie_usage_count(filename)
        if usage_count < 3: 
            available_cookies.append(filepath)
    
    if not available_cookies:
        return None, "All cookies have reached maximum usage limit (3 uses). Please upload more cookies."
    
    random.shuffle(available_cookies)
    filepath = available_cookies[0]
    filename = os.path.basename(filepath)
    
    cookie_str, session_token, user_up_token, device_id = parse_cookie_file(filepath)
    
    if not cookie_str:
        return None, f"Failed to parse {filename}"
    
    name = filename.replace('.txt', '').split('_')[0] if '_' in filename else filename.replace('.txt', '')
    
    return {
        'name': name,
        'cookie_str': cookie_str,
        'token': session_token or user_up_token,
        'device_id': device_id,
        'filepath': filepath,
        'filename': filename
    }, None

def get_cookie_count():
    return len(glob.glob("cookies/*.txt"))

def get_available_cookie_count():
    cookie_files = glob.glob("cookies/*.txt")
    available = 0
    for filepath in cookie_files:
        filename = os.path.basename(filepath)
        usage_count = get_cookie_usage_count(filename)
        if usage_count < 3:
            available += 1
    return available

def get_cookie_stats():
    cookie_files = glob.glob("cookies/*.txt")
    used_cookies = 0
    available = 0
    usage_details = []
    
    for filepath in cookie_files:
        filename = os.path.basename(filepath)
        usage_count = get_cookie_usage_count(filename)
        if usage_count >= 3:
            used_cookies += 1
        else:
            available += 1
        usage_details.append(f"• {filename}: {usage_count}/3 uses")
    
    return {
        'total': len(cookie_files),
        'available': available,
        'used': used_cookies,
        'details': usage_details
    }

def delete_cookie_file(filepath):
    try:
        os.remove(filepath)
        return True
    except:
        return False

def clear_all_cookies():
    cookie_files = glob.glob("cookies/*.txt")
    deleted = 0
    for filepath in cookie_files:
        if delete_cookie_file(filepath):
            deleted += 1
    save_cookie_usage({})
    return deleted

def escape_markdown(text):
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def activate_tv(qr_url, cookie_info):
    scraper = cloudscraper.create_scraper()
    
    headers = {
        "authority": "www.hotstar.com",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-MM,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
        "cookie": cookie_info['cookie_str'],
        "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"
    }
    
    if cookie_info.get('token'):
        headers["cookie"] += f"; userUP={cookie_info['token']}"
    
    try:
        response = scraper.get(qr_url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            if "success" in response.text.lower() or "activated" in response.text.lower():
                return True, "✅ JioHotstar Activation Successful"
            elif "JioHotstar" in response.text and "Watch TV Shows" in response.text:
                return True, "✅ Page loaded! TV activation in progress"
            else:
                return True, "✅ Request completed. Check your TV"
        
        return False, f"❌ Failed with status: {response.status_code}"
        
    except Exception as e:
        return False, f"❌ Error: {str(e)[:100]}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"📺 *JioHotstar QR Activator Bot*\n\n"
            f"👋 Welcome!\n\n"
            f"🔹 *Commands:*\n"
            f"/activate - Reply to the QR code Image\n"
            f"/upload - Admins Only Command\n"
            f"/stats - Check bot statistics\n"
            f"/clearall - Clear all cookies from vault\n\n"
            f"📂 *Cookie Vault:* {get_cookie_count()} accounts available\n"
            f"🟢 *Available:* {get_available_cookie_count()} accounts\n\n"
            f"• Bot Made By @KindCoders"
        ),
        parse_mode='Markdown',
        reply_to_message_id=update.message.message_id
    )

async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📸 *Please reply to this message with a JioHotstar QR code image*",
        parse_mode='Markdown',
        reply_to_message_id=update.message.message_id
    )
    context.user_data['activate_msg_id'] = msg.message_id

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⛔ *Unauthorized access!*",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        return
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📤 *Upload Cookies*\n\nSend me a ZIP file containing cookie files (.txt).\nThe files will be extracted to the cookie vault.",
        parse_mode='Markdown',
        reply_to_message_id=update.message.message_id
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⛔ *Unauthorized*",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        return
    
    stats = load_stats()
    cookie_stats = get_cookie_stats()
    
    stats_text = (
        f"📊 *Bot Statistics*\n\n"
        f"📈 *Activations:*\n"
        f"• Total: {stats['total_activations']}\n"
        f"• Successful: {stats['successful_activations']} ✅\n"
        f"• Failed: {stats['failed_activations']} ❌\n\n"
        f"📁 *Cookie Vault:*\n"
        f"• Total: {cookie_stats['total']}\n"
        f"• Available: {cookie_stats['available']} 🟢\n"
        f"• Used (3/3): {cookie_stats['used']} 🔴\n\n"
        f"📅 *Last Activation:* {stats.get('last_activation', 'Never')}"
    )
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=stats_text,
        parse_mode='Markdown',
        reply_to_message_id=update.message.message_id
    )

async def clearall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⛔ *Unauthorized access!*",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        return
    
    deleted = clear_all_cookies()
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🗑️ *Cleared All Cookies*\n\n✅ Deleted {deleted} cookie files from vault.\n📂 Vault is now empty.",
        parse_mode='Markdown',
        reply_to_message_id=update.message.message_id
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message.reply_to_message:
            return
        
        activate_msg_id = context.user_data.get('activate_msg_id')
        replied_msg_id = update.message.reply_to_message.message_id
        
        if not activate_msg_id or replied_msg_id != activate_msg_id:
            return
        
        processing_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔍 *Scanning Your QR Code...*",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        
        photo_file = await update.message.photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()
        
        files = {'file': ('qr_image.jpg', BytesIO(image_bytes), 'image/jpeg')}
        response = requests.post('https://api.qrserver.com/v1/read-qr-code/', files=files, timeout=15)
        
        if response.status_code != 200:
            await processing_msg.edit_text(
                "❌ *Failed to scan QR code*\n\nPlease try again with a clearer image.",
                parse_mode='Markdown'
            )
            update_stats(success=False)
            return
        
        data = response.json()
        if not data or not data[0]['symbol'][0]['data']:
            await processing_msg.edit_text(
                "❌ *No QR code found*\n\nPlease send a valid QR code image.",
                parse_mode='Markdown'
            )
            update_stats(success=False)
            return
        
        qr_data = data[0]['symbol'][0]['data']
        
        if 'hotstar.com' not in qr_data:
            await processing_msg.edit_text(
                "❌ *Invalid QR Code*\n\nThis doesn't appear to be a JioHotstar QR code.\nPlease send QR codes from hotstar.com/qr only.",
                parse_mode='Markdown'
            )
            update_stats(success=False)
            return
        
        await processing_msg.edit_text(
            "🔍 *Finding a working account for you...*",
            parse_mode='Markdown'
        )
        
        cookie_info, error = get_random_cookie()
        
        if not cookie_info:
            await processing_msg.edit_text(
                f"❌ *No working accounts available*\n\n{error}\n\n💡 Please contact the admin to add more accounts.",
                parse_mode='Markdown'
            )
            update_stats(success=False)
            return
        
        name_escaped = escape_markdown(cookie_info['name'])
        usage_count = get_cookie_usage_count(cookie_info['filename'])
        
        await processing_msg.edit_text(
            f"✅ *Found a suitable account for your request*\n\n"
            f"👤 Account: {name_escaped}\n"
            f"📊 Usage: {usage_count}/3",
            parse_mode='Markdown'
        )
        
        await processing_msg.edit_text(
            "🔄 *Activating your TV...*\n\nPlease wait while I connect your device.",
            parse_mode='Markdown'
        )
        
        success, message = activate_tv(qr_data, cookie_info)

        if success:
            new_usage = increment_cookie_usage(cookie_info['filename'])
            stats = update_stats(success=True)
            
            if new_usage >= 3:
                delete_cookie_file(cookie_info['filepath'])
                await processing_msg.edit_text(
                    f"⚠️ *Cookie reached maximum usage*\n\n"
                    f"Account {name_escaped} has been used {new_usage} times.\n"
                    f"It has been automatically removed from the vault.",
                    parse_mode='Markdown'
                )
                processing_msg = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="🔄 *Continuing with result...*",
                    parse_mode='Markdown'
                )
        else:
            stats = update_stats(success=False)
        
        qr_id = qr_data.split('qr_code=')[1].split('&')[0] if 'qr_code=' in qr_data else "N/A"
        
        if success:
            result_text = (
                f"🎉 *JioHotstar Activation Successful*\n\n"
                f"📱 *URL:* `{escape_markdown(qr_data[:80])}...`\n"
                f"📊 *Resp:* Succeeded\n"
                f"👤 *Account:* {name_escaped}\n"
                f"🆔 *QR ID:* `{escape_markdown(qr_id)}`\n"
                f"📊 *Usage:* {new_usage if success else usage_count}/3\n\n"
                f"🔥 *Thanks for using the bot!*\n✨ Your TV should now be connected."
            )
        else:
            result_text = (
                f"❌ *Activation Failed*\n\n"
                f"{message}\n\n"
                f"🔄 Please try again or use a different QR code."
            )
        
        await processing_msg.edit_text(result_text, parse_mode='Markdown')
        context.user_data.pop('activate_msg_id', None)
            
    except Exception as e:
        logger.error(f"Error: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ *Error*\n\n{str(e)[:200]}",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        context.user_data.pop('activate_msg_id', None)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⛔ *Unauthorized access!*",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        return
    
    document = update.message.document
    
    if not document.file_name.endswith('.zip'):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ *Please send a ZIP file*",
            parse_mode='Markdown',
            reply_to_message_id=update.message.message_id
        )
        return
    
    processing_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📥 *Processing ZIP file...*",
        parse_mode='Markdown',
        reply_to_message_id=update.message.message_id
    )
    
    try:
        file = await document.get_file()
        zip_path = f"temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        await file.download_to_drive(zip_path)
        
        extract_path = f"temp_extract_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        os.makedirs(extract_path, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_path)
        
        txt_files = glob.glob(f"{extract_path}/*.txt")
        added_count = 0
        
        for txt_file in txt_files:
            filename = os.path.basename(txt_file)
            dest_path = os.path.join("cookies", filename)
            if os.path.exists(dest_path):
                name, ext = os.path.splitext(filename)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                dest_path = os.path.join("cookies", f"{name}_{timestamp}{ext}")
            shutil.move(txt_file, dest_path)
            added_count += 1
        
        shutil.rmtree(extract_path)
        os.remove(zip_path)
        
        total_cookies = get_cookie_count()
        available_cookies = get_available_cookie_count()
        
        await processing_msg.edit_text(
            f"✅ *Cookies Uploaded Successfully!*\n\n"
            f"📦 Added: {added_count} files\n"
            f"📊 Total in vault: {total_cookies}\n"
            f"🟢 Available: {available_cookies}\n\n"
            f"🔄 The new cookies are now available for activation.",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await processing_msg.edit_text(
            f"❌ *Upload failed*\n\n{str(e)[:200]}",
            parse_mode='Markdown'
        )

def main():
    os.makedirs("cookies", exist_ok=True)
    
    print("\n" + "="*80)
    print("🤖 JIOHOTSTAR TV ACTIVATOR BOT")
    print("="*80)
    print(f"📁 Cookies folder: {os.path.abspath('cookies')}")
    print(f"📊 Total cookies: {get_cookie_count()}")
    print(f"🟢 Available: {get_available_cookie_count()}")
    print(f"👑 Admin ID: {ADMIN_ID}")
    print("="*80 + "\n")
    
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("activate", activate_command))
    application.add_handler(CommandHandler("upload", upload_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("clearall", clearall_command))

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    print("🤖 Bot is running...")
    print("📱 Press Ctrl+C to stop\n")
    
    application.run_polling()

if __name__ == '__main__':
    main()