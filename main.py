import asyncio
import os

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.files import JSONStorage
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.utils.markdown import text, hlink
from aiohttp import ClientSession
from loguru import logger
from dotenv import load_dotenv

from sqliter import SQLighter
from utils import timestamp

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
BITQUERY_API_KEY = os.getenv('BITQUERY_API_KEY')
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')

BITQUERY_URL = 'https://graphql.bitquery.io/'
GET_TRANSFERS_QUERY = 'query ($network: EthereumNetwork!, $hash: String!, $limit: Int!, $offset: Int!) {\n  ethereum(network: $network) {\n    transfers(\n      options: {limit: $limit, offset: $offset}\n      amount: {gt: 0}\n      txHash: {is: $hash}\n    ) {\n      sender {\n        address\n        annotation\n      }\n      receiver {\n        address\n        annotation\n      }\n      amount\n      currency {\n        symbol\n        address\n        tokenType\n        name\n      }\n    }\n  }\n}\n'
GET_TXS_QUERY = 'query ($address: String!, $block: Int!) {\n  ethereum(network: ethereum) {\n    transactions(\n      height: {gt: $block}\n      options: {limit: 10, offset: 0}\n      any: [{txSender: {is: $address}}, {txTo: {is: $address}}]\n    ) {\n      block {\n        height\n        timestamp {\n          unixtime\n        }\n      }\n      hash\n      sender {\n        address\n      }\n      to {\n        address\n      }\n      success\n      value: amount\n      amount(in: USD)\n      eth_fee: gasValue\n      usd_fee: gasValue(in: USD)\n    }\n  }\n}\n'

EXPLORER_TX_URL = 'https://etherscan.com/tx/%s'
EXPLORER_ADDRESS_URL = 'https://etherscan.com/address/%s'
EXPLORER_NFT_URL = 'https://etherscan.com/token/%s'

LAST_BLOCK_URL = f'https://api.etherscan.io/api?module=proxy&action=eth_blockNumber&apikey={ETHERSCAN_API_KEY}'
ADDRESS_CHECK_URL = f'https://api.etherscan.io/api?module=account&action=balance&address=%s&tag=latest&apikey={ETHERSCAN_API_KEY}'

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


@logger.catch
async def get_transactions(address: str, last_block: int, session: ClientSession):
    variables = '{\n  \"address\": \"%s\",\n  \"block\": %d\n}'
    async with session.post(BITQUERY_URL, headers={"X-API-KEY": BITQUERY_API_KEY},
                            json={'query': GET_TXS_QUERY, 'variables': variables % (address, last_block)}) as res:
        return await res.json()


@logger.catch
async def get_tx_transfers(tx_hash: str, session: ClientSession):
    variables = '{\n  \"limit\": 10,\n  \"offset\": 0,\n  \"network\": \"ethereum\",\n  \"hash\": \"%s\"\n}'
    async with session.post(BITQUERY_URL, headers={"X-API-KEY": BITQUERY_API_KEY},
                            json={'query': GET_TRANSFERS_QUERY, 'variables': variables % tx_hash}) as res:
        return await res.json()


async def _get_nft_transfers(tx: dict, session: ClientSession):
    transfers = await get_tx_transfers(tx['hash'], session)
    transfers = transfers['data']['ethereum']['transfers']
    result = []
    if not transfers:
        return result
    for transfer in transfers:
        if (transfer['currency']['tokenType'] == 'ERC721' or transfer['currency']['tokenType'] == 'ERC20') and (
                transfer['currency']['symbol'] != 'WETH' and transfer['currency']['symbol'] != 'ETH'):
            result.append(transfer)
    return result


def _form_transfers(transfers: list):
    result = 'NFT Transferred:'
    for transfer in transfers:
        nft_name = transfer["currency"]["name"] if transfer["currency"]["name"] != '-' or not transfer["currency"][
            "name"] else transfer["currency"]["address"]
        result += text('',
                       f'From➡: {hlink(transfer["sender"]["address"], EXPLORER_ADDRESS_URL % transfer["sender"]["address"])}',
                       f'To⬅: {hlink(transfer["receiver"]["address"], EXPLORER_ADDRESS_URL % transfer["receiver"]["address"])}',
                       f'NFT: {hlink(nft_name, EXPLORER_NFT_URL % transfer["currency"]["address"])}',
                       sep='\n') + '\n'
    return result


def _form_message(tx: dict, wallet, transfers):
    status = 'Success' if tx['success'] else 'Failed'
    message = text(f'Wallet tracked: {hlink(wallet[3], EXPLORER_ADDRESS_URL % wallet[0])}',
                   hlink('New Transfer!', EXPLORER_TX_URL % tx['hash']),
                   f'From➡: {hlink(tx["sender"]["address"], EXPLORER_ADDRESS_URL % tx["sender"]["address"])}',
                   f'To⬅: {hlink(tx["to"]["address"], EXPLORER_ADDRESS_URL % tx["to"]["address"])}',
                   f'Status: {status}',
                   f'Timestamp: {timestamp(tx["block"]["timestamp"]["unixtime"])}',
                   _form_transfers(transfers),
                   f'Transaction Value💰: {round(tx["value"], 5)} Ether (${round(tx["amount"], 2)})',
                   f'Transaction Fee💲: {round(tx["eth_fee"], 5)} Ether (${round(tx["usd_fee"], 2)})',
                   sep='\n\n')
    return message


@logger.catch
async def new_tx_alert(txs: list, wallet, session: ClientSession):
    for tx in txs:
        transfers = await _get_nft_transfers(tx, session)
        if not transfers:
            continue
        notification = _form_message(tx, wallet, transfers)
        logger.info('New Transfer Alert!')
        await bot.send_message(wallet[2], notification)

    db.update_block(txs[-1]['block']['height'], wallet[0], wallet[2])
    logger.info(f"Block updated to {txs[-1]['block']['height']}")


@logger.catch
async def track_wallets():
    logger.info('Started Tracking')
    while True:
        session = ClientSession()
        try:
            for wallet in db.get_all_wallets():
                txs = await get_transactions(wallet[0], wallet[1], session)
                if txs and txs['data']['ethereum']['transactions']:
                    logger.info(f'New tx to {wallet[0]}')
                    await new_tx_alert(txs['data']['ethereum']['transactions'], wallet, session)
                await asyncio.sleep(2)
            await session.close()
            await asyncio.sleep(5)
        except Exception as e:
            logger.exception(f'Exception', exc_info=e)
            await session.close()
            await asyncio.sleep(5)


async def on_bot_start_up(dispatcher) -> None:
    """List of actions which should be done before bot start"""
    logger.info('Start up')
    asyncio.create_task(track_wallets())


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_bot_start_up)
