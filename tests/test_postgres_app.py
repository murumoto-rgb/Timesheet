"""Real Postgres checks for serverless shared state; no QuickBooks network."""
import os
from urllib.parse import urlparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from cryptography.fernet import Fernet
import psycopg
import main
from postgres_store import PostgresStore
from write_journal import Journal

pytestmark = pytest.mark.skipif(not os.environ.get('TIMESHEET_TEST_DATABASE_URL'), reason='local Postgres not configured')

@pytest.fixture
def shared_store(monkeypatch):
    url = os.environ['TIMESHEET_TEST_DATABASE_URL']
    assert urlparse(url).hostname in {'localhost', '127.0.0.1', '::1'}, 'destructive tests require loopback Postgres'
    assert urlparse(url).path == '/timesheet_test', 'dedicated test database required'
    with psycopg.connect(url, autocommit=True) as c:
        c.execute('TRUNCATE timesheet_private.encrypted_blobs')
    key=Fernet.generate_key()
    store=PostgresStore(url,key,allow_insecure_local=True)
    monkeypatch.setattr(main,'DATABASE_URL',url)
    monkeypatch.setattr(main,'_postgres',lambda:store)
    monkeypatch.setattr(main,'_load_tokens',lambda:store.load(1))
    return store


def test_simultaneous_refresh_is_once_and_durable(shared_store,monkeypatch):
    store=shared_store
    store.save(1,{'realm_id':'synthetic','access_token':'expired','refresh_token':'old','access_expires_at':0})
    calls=[]
    def refresh(payload):
        calls.append(payload)
        time.sleep(.05)
        return {'access_token':'fresh','refresh_token':'rotated','expires_in':3600}
    monkeypatch.setattr(main,'_token_request',refresh)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:main.get_access_token(),range(4)))
    assert len(calls)==1
    assert results==[('fresh','synthetic')]*4
    assert store.load(1)['refresh_token']=='rotated'


def test_journal_commits_before_external_side_effect_even_when_outer_call_raises(shared_store):
    import uuid
    operation=str(uuid.uuid4()); payload={'synthetic':True}
    journal=Journal('unused',store=shared_store)
    with pytest.raises(RuntimeError):
        with shared_store.lock('tokens'):
            # Simulate reservation before HTTP, then uncertain client failure.
            claim=journal.claim(operation,'create',payload,'sandbox|synthetic')
            assert claim
            # Another connection sees the journal BEFORE releasing token lock.
            with psycopg.connect(shared_store._url) as c:
                assert c.execute('SELECT count(*) FROM timesheet_private.encrypted_blobs WHERE id=4').fetchone()[0]==1
            raise RuntimeError('synthetic response failure')
    assert journal.read()['operations'][0]['operationId']==operation


def test_company_replacement_waits_for_other_instance_lock(shared_store):
    entered=threading.Event(); release=threading.Event(); switched=threading.Event()
    shared_store.save(1,{'realm_id':'synthetic-a'})
    def writing():
        with shared_store.lock('tokens'):
            entered.set(); assert release.wait(3)
            assert shared_store.load(1)['realm_id']=='synthetic-a'
    def replacing():
        main._save_tokens({'realm_id':'synthetic-b'}); switched.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(writing); assert entered.wait(3)
        second=pool.submit(replacing); assert not switched.wait(.1)
        release.set(); first.result(3); second.result(3)
    assert shared_store.load(1)['realm_id']=='synthetic-b'


def test_single_use_oauth_state_across_connections(shared_store, monkeypatch):
    from fastapi import HTTPException
    shared_store.save(5,{'synthetic-state':time.time()})
    calls=[]
    def exchange(payload):
        calls.append(payload)
        return {'access_token':'synthetic-access','refresh_token':'synthetic-refresh','expires_in':3600}
    monkeypatch.setattr(main,'_token_request',exchange)
    def callback():
        try:
            return main.callback(code='synthetic-code',state='synthetic-state',realmId='synthetic-realm').status_code
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:callback(),range(2)))
    assert sorted(results)==[307,400]
    assert len(calls)==1
    assert shared_store.load(5)=={}


def test_reminder_claim_across_connections(shared_store,monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from unittest.mock import Mock
    clock=Mock(); clock.now.return_value=datetime(2026,10,6,18,tzinfo=ZoneInfo('America/Chicago'))
    monkeypatch.setattr(main,'datetime',clock)
    shared_store.save(2,{'subs':[{'endpoint':'https://synthetic.invalid'}]})
    sends=[]
    monkeypatch.setattr(main,'_notify_all',lambda: sends.append(True) or 1)
    monkeypatch.setattr(main,'_logged_time_today',lambda _:False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:main._reminder_tick(),range(4)))
    assert len(sends)==1
    assert shared_store.load(2)['last_reminder']=='2026-10-06'


def test_parallel_journal_claim_has_one_owner(shared_store):
    import uuid
    operation=str(uuid.uuid4()); payload={'synthetic':True}
    def claim():
        try:
            return Journal('unused',store=shared_store).claim(operation,'create',payload,'sandbox|synthetic')
        except Exception as exc:
            return exc
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:claim(),range(4)))
    assert len(shared_store.load(4)['operations'])==1
    assert sum(isinstance(result,dict) for result in results)==1


def test_audit_parallel_appends_do_not_lose_history(shared_store):
    shared_store.save(3, {'events': []})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: main._audit('create', str(n), {}, scope='sandbox|synthetic'), range(12)))
    assert all(results)
    events = shared_store.load(3)['events']
    assert len(events) == 12
    assert {e['entryId'] for e in events} == {str(n) for n in range(12)}


def test_bootstrap_import_rolls_back_if_later_insert_fails(shared_store):
    from fastapi import HTTPException
    with shared_store._connection() as c:
        c.execute("ALTER TABLE timesheet_private.encrypted_blobs ADD CONSTRAINT synthetic_reject_id_two CHECK (id <> 2)")
    try:
        with pytest.raises(HTTPException):
            shared_store.import_empty({1: {'synthetic': 'first'}, 2: {'synthetic': 'fail'}})
        assert shared_store.export_blobs() == {}
    finally:
        with shared_store._connection() as c:
            c.execute("ALTER TABLE timesheet_private.encrypted_blobs DROP CONSTRAINT synthetic_reject_id_two")
