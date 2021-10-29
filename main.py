import asyncio
import os

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.files import JSONStorage
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.utils.markdown import text, hlink
from aiohttp import ClientSession
from dotenv import load_dotenv
from loguru import logger

from sqliter import SQLighter
from utils import timestamp

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
BITQUERY_API_KEY = os.getenv('BITQUERY_API_KEY')
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')

BITQUERY_URL = 'https://graphql.bitquery.io/'
TX_FEES = 'query ($hash: String!) {\n  ethereum(network: ethereum) {\n    transactions(txHash: {is: $hash}, options: {limit: 1}) {\n      value: amount\n      usd_value: amount(in: USD)\n      fee: gasValue\n      usd_fee: gasValue(in: USD)\n    }\n  }\n}\n'

EXPLORER_TX_URL = 'https://etherscan.com/tx/%s'
EXPLORER_ADDRESS_URL = 'https://etherscan.com/address/%s'
EXPLORER_NFT_URL = 'https://etherscan.com/token/%s'

LAST_BLOCK_URL = f'https://api.etherscan.io/api?module=proxy&action=eth_blockNumber&apikey={ETHERSCAN_API_KEY}'
ADDRESS_CHECK_URL = f'https://api.etherscan.io/api?module=account&action=balance&address=%s&tag=latest&apikey={ETHERSCAN_API_KEY}'
NFT_TXS_URL = 'https://api.etherscan.io/api?module=account&action=tokennfttx&address=%s&startblock=%d&endblock=999999999&sort=asc&apikey=%s'
ZERO_ADDRESS = '0x0000000000000000000000000000000000000000'

OPENSEA_NFT_URL = 'https://opensea.io/assets/{}/{}'

logger.add('app.log', format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot, storage=JSONStorage('states.json'))

db = SQLighter('wallets')


class Menu(StatesGroup):
    menu = State()
    add_wallet = State()
    remove_wallet = State()
    see_wallets = State()


@dp.message_handler(commands=['eth'], state='*')
async def menu(message: types.Message):
    kb = InlineKeyboardMarkup(row_width=1)
    new = InlineKeyboardButton('Add wallet', callback_data='add')
    remove = InlineKeyboardButton('Remove wallet', callback_data='remove')
    see = InlineKeyboardButton('See wallets', callback_data='see')
    kb.add(new, remove, see)
    await message.reply('Please, select action', reply_markup=kb)


@dp.callback_query_handler(lambda query: query.data == 'add', state='*')
async def add(query: types.CallbackQuery):
    await query.message.answer('Send wallet name and a 42 character eth wallet address, format: "name address"')
    await Menu.add_wallet.set()
    await query.answer()


@dp.message_handler(state=Menu.add_wallet)
async def process_add(message: types.Message):
    wallet = message.text.split()
    if len(wallet) != 2:
        await message.answer('Invalid syntax, should be "name address". Try again:')
        return
    session = ClientSession()
    if not await address_exists(wallet[1], session):
        await message.answer('Invalid wallet address, try again:')
        await session.close()
        return
    if db.wallet_exists(wallet[1], message.chat.id):
        await message.answer('Wallet already added, try again:')
        await session.close()
        return
    block = await get_last_block(session)
    await session.close()
    db.add_wallet(wallet[0], block, wallet[1], message.chat.id)
    logger.info(f'Wallet added {wallet[0]}')
    await message.answer('New wallet added!')
    await Menu.menu.set()


@dp.callback_query_handler(lambda query: query.data == 'remove', state='*')
async def del_wallet(query: types.CallbackQuery):
    addresses = db.get_tracking_wallets(query.message.chat.id)
    markup = InlineKeyboardMarkup()
    buttons = (InlineKeyboardButton((address[0]), callback_data=address[0]) for address in addresses)
    markup.add(*buttons)
    await Menu.remove_wallet.set()
    response = 'Tap the wallet you want to remove:'
    await query.message.answer(response, reply_markup=markup)
    await query.answer()


@dp.callback_query_handler(state=Menu.remove_wallet)
async def process_del(query: types.CallbackQuery):
    wallet = query.data
    if not db.wallet_exists(wallet, query.message.chat.id):
        await query.message.answer('Wallet not added yet')
        return
    db.delete_wallet(wallet, query.message.chat.id)
    logger.info(f'Wallet deleted {wallet}')
    await query.message.answer('Wallet deleted!')
    await query.answer()
    await query.message.delete()
    await Menu.menu.set()


@dp.callback_query_handler(lambda query: query.data == 'see', state='*')
async def see_wallets(query: types.CallbackQuery):
    addresses = db.get_tracking_wallets(query.message.chat.id)
    result = 'Tracked Wallets:\n\n'
    for address in addresses:
        result += f'{address[0]}:\n{address[1]}\n'
    await query.message.answer(result)
    await query.answer()


@logger.catch
async def address_exists(address: str, session: ClientSession):
    async with session.get(ADDRESS_CHECK_URL % address) as res:
        result = await res.json()
        return result['status'] == "1"


@logger.catch
async def get_last_block(session: ClientSession):
    async with session.get(LAST_BLOCK_URL) as res:
        result = await res.json()
        return int(result['result'], base=16)


async def get_nft_transfers(address: str, start_block: int, session: ClientSession):
    async with session.get(NFT_TXS_URL % (address, start_block, ETHERSCAN_API_KEY)) as res:
        try:
            return await res.json()
        except Exception as e:
            logger.error(f'Error in get_nft_transfers: {e}')
            logger.error(await res.text())


@logger.catch
async def _get_tx_fees(tx_hash: str, session: ClientSession):
    variables = '{\"hash\": \"%s\"}'
    async with session.post(BITQUERY_URL, headers={"X-API-KEY": BITQUERY_API_KEY},
                            json={'query': TX_FEES, 'variables': variables % tx_hash}) as res:
        fees = await res.json()
        return fees['data']['ethereum']['transactions'][0]


async def _form_message(transfer: dict, wallet, session: ClientSession):
    header = 'New Mint!' if transfer["from"] == ZERO_ADDRESS else 'New Transfer!'
    nft = f'{transfer["tokenName"]} #{transfer["tokenID"]}'
    fees = await _get_tx_fees(transfer['hash'], session)
    logger.info(f'Tx hash: {transfer["hash"]}')
    logger.info(f'Fees: {fees}')
    message = text(f'Wallet tracked: {hlink(wallet[3], EXPLORER_ADDRESS_URL % wallet[0])}',
                   hlink(header, EXPLORER_TX_URL % transfer['hash']),
                   f'From➡: {hlink(transfer["from"], EXPLORER_ADDRESS_URL % transfer["from"])}',
                   f'To⬅: {hlink(transfer["to"], EXPLORER_ADDRESS_URL % transfer["to"])}',
                   f'NFT: {hlink(nft, OPENSEA_NFT_URL.format(transfer["contractAddress"], transfer["tokenID"]))}',
                   'Status: Success',
                   f'Timestamp: {timestamp(transfer["timeStamp"])}',
                   f'Transaction Value💰: {round(fees["value"], 5)} Ether (${round(fees["usd_value"], 2)})',
                   f'Transaction Fee💲: {round(fees["fee"], 5)} Ether (${round(fees["usd_fee"], 2)})',
                   sep='\n\n')
    return message


@logger.catch
async def new_tx_alert(transfers: list, wallet, session: ClientSession):
    for transfer in transfers:
        if transfer["tokenID"] == '0':
            continue
        notification = await _form_message(transfer, wallet, session)
        await bot.send_message(wallet[2], notification)
        logger.info('New Transfer Alert!')
        await asyncio.sleep(0.5)

    new_block = int(transfers[-1]['blockNumber']) + 1
    db.update_block(new_block, wallet[0], wallet[2])
    logger.info(f"Block updated to {new_block}")


@logger.catch
async def track_wallets():
    logger.info('Started Tracking')
    while True:
        session = ClientSession()
        try:
            for wallet in db.get_all_wallets():
                transfers = await get_nft_transfers(wallet[0], wallet[1], session)
                if transfers and transfers['result']:
                    logger.info(f'New nft transfers to {wallet[3]}')
                    await new_tx_alert(transfers['result'], wallet, session)
                await asyncio.sleep(2)
            await session.close()
            await asyncio.sleep(5)
        except Exception as e:
            logger.exception(f'Exception while tracking:', exc_info=e)
            await session.close()
            await asyncio.sleep(2)


async def on_bot_start_up(dispatcher) -> None:
    """List of actions which should be done before bot start"""
    logger.info('Start up')
    asyncio.create_task(track_wallets())


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_bot_start_up)
