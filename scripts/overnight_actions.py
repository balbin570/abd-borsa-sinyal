"""Run the EXISTING Overnight V3 workers directly on GitHub Actions (no Render HTTP)."""
import argparse
import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')

def check_env(mode):
    required = ['DATABASE_URL', 'TWELVE_DATA_API_KEY']
    if mode in ('final', 'telegram-test'):
        required += ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError('Missing GitHub Actions secrets: ' + ', '.join(missing))

async def run(mode, manual):
    from app import main as m
    now = datetime.now(NY)
    day = now.date().isoformat()
    if mode != 'telegram-test' and not manual:
        if now.weekday() >= 5:
            print('Not an NY weekday; skipped')
            return
        minute = now.hour * 60 + now.minute
        windows = {'prescreen': (13*60+40, 14*60+10), 'final': (15*60+5, 15*60+20)}
        lo, hi = windows[mode]
        if not lo <= minute <= hi:
            print(f'Outside {mode} NY launch window: {now.isoformat()}; skipped')
            return
    if mode == 'telegram-test':
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                                  json={'chat_id': os.environ['TELEGRAM_CHAT_ID'],
                                        'text': f'🧪 GitHub Actions Overnight V3 bağlantı testi\n{now.isoformat()}\nSinyal değildir.'})
            r.raise_for_status()
            if not r.json().get('ok'):
                raise RuntimeError('Telegram did not acknowledge test')
        print('Telegram test confirmed')
        return
    if mode == 'prescreen':
        existing = await asyncio.to_thread(m._overnight_pre_db, 'read', day)
        if existing and existing.get('status') == 'COMPLETED' and existing.get('scanned_count') == len(m._OVERNIGHT_SYMBOLS):
            print(f'Prescreen already complete: {day}')
            return
        await m._overnight_prescreen_worker(day)
        if m._OVERNIGHT_PRE_STATE.get('last_error'):
            raise RuntimeError(m._OVERNIGHT_PRE_STATE['last_error'])
        saved = await asyncio.to_thread(m._overnight_pre_db, 'read', day)
        if not saved or saved.get('status') != 'COMPLETED' or saved.get('scanned_count') != len(m._OVERNIGHT_SYMBOLS):
            raise RuntimeError('Prescreen not complete or not persisted')
        print('Prescreen COMPLETE:', saved['scanned_count'], 'shortlist:', saved['shortlist'])
    else:
        pre = await asyncio.to_thread(m._overnight_pre_db, 'read', day)
        if not pre or pre.get('status') != 'COMPLETED' or pre.get('scanned_count') != len(m._OVERNIGHT_SYMBOLS):
            raise RuntimeError('No complete prescreen for today; refusing final')
        before = await asyncio.to_thread(m._overnight_db, 'read', day)
        if before and before[0] == 'SENT':
            print('Already sent for today; skipping duplicate')
            return
        await m._overnight_dispatch(day)
        saved = await asyncio.to_thread(m._overnight_db, 'read', day)
        if not saved or saved[0] != 'SENT':
            raise RuntimeError('Final not confirmed SENT: ' + str(m._OVERNIGHT_STATE.get('last_error')))
        print('Telegram confirmed:', saved[1].get('telegram_message_id'))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prescreen', 'final', 'telegram-test'])
    parser.add_argument('--manual', action='store_true', help='Bypass launch-time guard, not final delivery deadline')
    args = parser.parse_args()
    check_env(args.mode)
    try:
        asyncio.run(run(args.mode, args.manual))
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)

if __name__ == '__main__':
    main()
