# bot.py
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.bot import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage # <--- ИМПОРТИРУЙ ЭТО

from config import API_TOKEN

# Настройка логирования для отладки (можно INFO или DEBUG)
logging.basicConfig(
    level=logging.INFO, # Поставь DEBUG для максимальной детализации
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
# Отключаем слишком подробные логи от http-клиента, если нужно
# logging.getLogger('httpx').setLevel(logging.WARNING)
# logging.getLogger('aiogram.client.session.aiohttp').setLevel(logging.WARNING)


bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)

# Создаем storage
storage = MemoryStorage() # <--- СОЗДАЙ ЭКЗЕМПЛЯР STORAGE

dp = Dispatcher(storage=storage) # <--- ПЕРЕДАЙ STORAGE В ДИСПЕТЧЕР

# Далее твой код подключения роутеров и запуска, если он был здесь
# Например, в main.py ты делаешь dp.include_router(...) и dp.start_polling(...)