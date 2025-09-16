import os
import logging
import threading
import asyncio
import time
import groq
from flask import Flask, jsonify, render_template
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

import dotenv
dotenv.load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
PORT = int(os.getenv('PORT', 5000))

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable is required")
    exit(1)

# Initialize Groq client (if needed)
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
if GROQ_API_KEY:
    groq_client = groq.Client(api_key=GROQ_API_KEY)
    logger.info("Groq client initialized")
else:
    groq_client = None
    logger.warning("GROQ_API_KEY not set, Groq client not initialized")

# Global variables for bot management
telegram_app = None
bot_thread = None
bot_running = False


# Bot handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name if update.effective_user else "User"
    welcome_text = f"🤖 Hello {user_name}! I'm running on Render with polling!\n\n" 
    if update.message:
        await update.message.reply_text(welcome_text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message and update.message.text:
            text = update.message.text
            groq_response = None
            if groq_client:
                try:
                    groq_response = groq_client.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        messages=[
                            {
                                "role": "user",
                                "content": text
                            }
                        ]
                    )
                    reply_content = groq_response.choices[0].message.content or "No response from Groq."
                    await update.message.reply_text(reply_content)
                except Exception as ge:
                    logger.error(f"Error querying Groq: {ge}")
                    await update.message.reply_text("Error querying Groq service.")
        else:
            logger.warning("Received update without message or text.")
    except Exception as e:
        logger.error(f"Error in echo handler: {e}")
        if update.message:
            await update.message.reply_text("Sorry, I encountered an error processing your message.")

def setup_telegram_bot():
    """Initialize and configure the Telegram bot"""
    global telegram_app
    
    try:
        logger.info("Setting up Telegram bot...")
        if not TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required and must not be None")
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add handlers
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        logger.info("Telegram bot setup complete")
        return telegram_app
        
    except Exception as e:
        logger.error(f"Error setting up Telegram bot: {e}")
        return None

def run_telegram_bot():
    """Run the Telegram bot in a separate thread with polling"""
    global bot_running, bot_start_time, last_error
    
    def bot_worker():
        global bot_running, bot_start_time, last_error
        
        try:
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            logger.info("Starting Telegram bot with polling...")
            bot_running = True
            bot_start_time = time.time()
            last_error = None
            
            # Run the bot with polling
            if telegram_app is not None:
                telegram_app.run_polling(
                    drop_pending_updates=True,
                    close_loop=False,
                    stop_signals=None  # Don't stop on signals since we're in a thread
                )
            else:
                logger.error("Telegram app is not initialized. Cannot run polling.")
            
        except Exception as e:
            logger.error(f"Error in bot thread: {e}")
            bot_running = False
            last_error = str(e)
        finally:
            logger.info("Bot thread finished")
            bot_running = False
    
    # Start bot in background thread
    thread = threading.Thread(target=bot_worker, daemon=True, name="TelegramBotThread")
    thread.start()
    logger.info(f"Bot thread started: {thread.name}")
    return thread

# Flask routes (required for Render web service)
@app.route('/')
def index():
    """Landing page"""
    return render_template('index.html')

@app.route('/restart_bot', methods=['POST'])
def restart_bot():
    """Restart the bot (useful for debugging)"""
    global bot_thread, telegram_app, bot_running, last_error
    
    try:
        logger.info("Attempting to restart bot...")
        
        # Stop existing bot
        if telegram_app:
            try:
                if telegram_app.running:
                    logger.info("Stopping existing Telegram application...")
                    asyncio.run(telegram_app.stop())
                    time.sleep(2)  # Give it time to stop
            except Exception as e:
                logger.warning(f"Error stopping telegram app: {e}")
        
        # Wait for thread to finish
        if bot_thread and bot_thread.is_alive():
            logger.info("Waiting for bot thread to finish...")
            bot_thread.join(timeout=10)  # Increased timeout
            if bot_thread.is_alive():
                logger.warning("Bot thread did not stop gracefully")
        
        # Reset state
        bot_running = False
        last_error = None
        
        # Start new bot
        logger.info("Setting up new bot instance...")
        if setup_telegram_bot():
            logger.info("Starting new bot thread...")
            bot_thread = run_telegram_bot()
            time.sleep(2)  # Give it time to start
            
            return jsonify({
                "status": "Bot restarted successfully",
                "thread_id": bot_thread.ident if bot_thread else None
            })
        else:
            return jsonify({"status": "Error: Failed to setup bot"}), 500
    
    except Exception as e:
        logger.error(f"Error restarting bot: {e}")
        last_error = str(e)
        return jsonify({"status": f"Error: {e}"}), 500


# Initialize and start the bot when the module loads
def initialize_bot():
    """Initialize the bot on startup"""
    global bot_thread
    
    try:
        logger.info("Initializing Telegram bot...")
        
        if setup_telegram_bot():
            bot_thread = run_telegram_bot()
            logger.info("Bot initialization complete")
            
            # Give it a moment to start
            time.sleep(3)
            
            # Check if it started successfully
            if bot_thread and bot_thread.is_alive() and bot_running:
                logger.info("✅ Bot started successfully!")
            else:
                logger.error("❌ Bot failed to start properly")
        else:
            logger.error("❌ Failed to setup bot")
            
    except Exception as e:
        logger.error(f"❌ Error during bot initialization: {e}")

if __name__ == '__main__':
    # Initialize bot
    initialize_bot()
    
    # Run Flask app
    logger.info(f"Starting Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
else:
    # When running with gunicorn, initialize bot here
    initialize_bot()