# main.py
import asyncio
import logging

from bot import bot, dp
from modules.start import router as start_router
from modules.footle import router as footle_router
from modules.club_connect import router as ttt_router
from modules.duel import router as duel_router
from modules.solo_guess import router as solo_guess_router

logging.basicConfig(level=logging.WARNING)

async def main():
    dp.include_router(start_router)
    dp.include_router(footle_router)
    dp.include_router(solo_guess_router)
    dp.include_router(ttt_router)
    dp.include_router(duel_router)

    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())