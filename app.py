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
bot_start_time = None
last_error = None
shutdown_event = threading.Event()  # Event to signal shutdown

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
            if groq_client:
                try:
                    groq_response = groq_client.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        messages=[{"role": "user", "content": text}]
                    )
                    reply_content = groq_response.choices[0].message.content or "No response from Groq."
                    await update.message.reply_text(reply_content)
                except Exception as ge:
                    logger.error(f"Error querying Groq: {ge}")
                    await update.message.reply_text("Error querying Groq service.")
        else:
            logger.warning("Received update without message or text.")
    except Exception as e:
        logger.error(f"Error in message handler: {e}")
        if update.message:
            await update.message.reply_text("Sorry, I encountered an error processing your message.")

def setup_telegram_bot():
    """Initialize and configure the Telegram bot"""
    global telegram_app
    
    try:
        logger.info("Setting up Telegram bot...")
        if not TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
        
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add handlers
        telegram_app.add_handler(CommandHandler("start", start_command))
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
        global bot_running, bot_start_time, last_error, telegram_app
        
        loop = None
        try:
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            logger.info("Starting Telegram bot with polling...")
            bot_running = True
            bot_start_time = time.time()
            last_error = None
            
            if telegram_app is not None:
                # Start polling with proper error handling
                async def run_polling():
                    try:
                        await telegram_app.initialize() # type: ignore
                        await telegram_app.start() # type: ignore
                        await telegram_app.updater.start_polling( # type: ignore
                            drop_pending_updates=True
                        )
                        
                        # Keep running until shutdown is signaled
                        while not shutdown_event.is_set() and bot_running:
                            await asyncio.sleep(1)
                            
                    except Exception as e:
                        logger.error(f"Error in polling: {e}")
                        raise
                    finally:
                        # Cleanup
                        logger.info("Cleaning up telegram app...")
                        try:
                            if telegram_app and telegram_app.updater and telegram_app.updater.running:
                                await telegram_app.updater.stop()
                            if telegram_app and telegram_app.running:
                                await telegram_app.stop()
                            if telegram_app:
                                await telegram_app.shutdown()
                        except Exception as cleanup_error:
                            logger.error(f"Error during cleanup: {cleanup_error}")
                
                # Run the polling
                loop.run_until_complete(run_polling())
            else:
                logger.error("Telegram app is not initialized. Cannot run polling.")
                
        except Exception as e:
            logger.error(f"Error in bot thread: {e}")
            last_error = str(e)
        finally:
            logger.info("Bot thread finished")
            bot_running = False
            if loop and not loop.is_closed():
                try:
                    loop.close()
                except Exception as e:
                    logger.error(f"Error closing event loop: {e}")
    
    # Start bot in background thread
    thread = threading.Thread(target=bot_worker, daemon=False, name="TelegramBotThread")
    thread.start()
    logger.info(f"Bot thread started: {thread.name}")
    return thread

def stop_bot_gracefully():
    """Gracefully stop the bot and wait for thread to finish"""
    global bot_thread, telegram_app, bot_running, shutdown_event
    
    try:
        logger.info("Initiating graceful bot shutdown...")
        
        # Signal shutdown
        shutdown_event.set()
        bot_running = False
        
        # Wait for thread to finish
        if bot_thread and bot_thread.is_alive():
            logger.info("Waiting for bot thread to finish...")
            bot_thread.join(timeout=15)
            
            if bot_thread.is_alive():
                logger.warning("Bot thread did not stop gracefully within timeout")
                return False
            else:
                logger.info("Bot thread stopped gracefully")
                return True
        
        logger.info("Bot stopped successfully")
        return True
        
    except Exception as e:
        logger.error(f"Error during graceful shutdown: {e}")
        return False

# Flask routes
@app.route('/')
def index():
    """Landing page"""
    return render_template('index.html')

@app.route('/bot_status')
def bot_status():
    """Check bot status with detailed information"""
    global bot_thread, telegram_app
    
    # Check thread status
    thread_alive = bot_thread.is_alive() if bot_thread else False
    
    # Check application status
    app_running = False
    if telegram_app:
        try:
            app_running = telegram_app.running
        except Exception as e:
            logger.warning(f"Error checking app status: {e}")
            app_running = False
    
    # Calculate uptime
    uptime = int(time.time() - bot_start_time) if bot_start_time else 0
    
    status_data = {
        "bot_running": bot_running,
        "thread_alive": thread_alive,
        "application_running": app_running,
        "uptime_seconds": uptime,
        "uptime_formatted": f"{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s",
        "thread_name": bot_thread.name if bot_thread else None,
        "last_error": last_error,
        "bot_token_configured": bool(TELEGRAM_BOT_TOKEN),
        "shutdown_signaled": shutdown_event.is_set()
    }
    
    return jsonify(status_data)

@app.route('/start_bot', methods=['POST'])
def start_bot_endpoint():
    """Start the bot via web interface"""
    global bot_thread, telegram_app, shutdown_event
    
    try:
        if bot_running:
            return jsonify({"status": "Bot is already running"}), 400
        
        logger.info("Starting bot via web interface...")
        
        # Reset shutdown event
        shutdown_event.clear()
        
        # Setup and start bot
        if setup_telegram_bot():
            bot_thread = run_telegram_bot()
            time.sleep(2)  # Give it time to start
            
            if bot_thread and bot_thread.is_alive():
                return jsonify({
                    "status": "Bot started successfully",
                    "thread_id": bot_thread.ident
                })
            else:
                return jsonify({"status": "Bot failed to start"}), 500
        else:
            return jsonify({"status": "Failed to setup bot"}), 500
            
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        return jsonify({"status": f"Error: {e}"}), 500

@app.route('/stop_bot', methods=['POST'])
def stop_bot():
    """Stop the bot gracefully"""
    try:
        if not bot_running:
            return jsonify({"status": "Bot is not running"}), 400
            
        success = stop_bot_gracefully()
        if success:
            return jsonify({"status": "Bot stopped successfully"})
        else:
            return jsonify({"status": "Bot stop completed with warnings"}), 206
    except Exception as e:
        logger.error(f"Error stopping bot: {e}")
        return jsonify({"status": f"Error stopping bot: {e}"}), 500


# Initialize the bot
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
    # Run Flask app
    logger.info(f"Starting Flask app on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
