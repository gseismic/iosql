"""Example: store daily schedules using CalendarPartition."""

from pathlib import Path
import shutil

import arrow
from iosql import Database
from iosql.partition import CalendarPartition

DB_URI = 'sqlite://examples/runtime/partition/calendar_schedule.sqlite3'
DB_PATH = Path('examples/runtime/partition/calendar_schedule.sqlite3')


def run():
    if DB_PATH.exists():
        shutil.rmtree(DB_PATH)

    db = Database(DB_URI)
    partition = CalendarPartition(fmt='YYYYMMDD', tz='UTC', ts_col='event_time')

    events = [
        {
            'event_time': f'2025-01-{day:02d}T09:00:00+00:00',
            'title': f'Check-in {day}',
            'room': 'Conference Room B',
        }
        for day in range(1, 6)
    ]

    db['schedule'].insert_all(events, pk=['event_time', 'title'], partition=partition)

    print('Event partitions:', db['schedule'].table_parts())

    target = '2025-01-03T09:00:00+00:00'
    grouped = db['schedule'].select(
        '*',
        where={'field': 'event_time', 'op': '==', 'value': target},
        to_dict=True,
    )

    print('Events on 2025-01-03:', grouped)

    db.close()


if __name__ == '__main__':
    run()
