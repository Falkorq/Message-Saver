"""Compile real application functions without importing live configuration."""
import ast
import asyncio
import base64
import json
import logging
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextvars import ContextVar
from telebot import types
from telebot.asyncio_helper import ApiTelegramException
from reliable import AtomicConnection, DurableInbox, DeliveryBudget
from product_state import ProductState
from message_crypto import MessageCipher
from business_tools.repository import BusinessToolsRepository
from business_tools.service import BusinessToolsService
from profile_monitor.repository import ProfileRepository


class Harness:
    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(path, factory=AtomicConnection)
        self.db.row_factory = sqlite3.Row
        self.cipher = MessageCipher(base64.b64encode(b'x'*32).decode())
        self.routes = {100:1,300:3}
        tree = ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        schema = next(n.value.args[0].value for n in tree.body if isinstance(n,ast.Expr)
                      and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute)
                      and n.value.func.attr=='executescript')
        self.db.executescript(schema)
        self.repo = BusinessToolsRepository(self.db,self.routes,self.cipher);self.repo.migrate()
        ProfileRepository(self.db).migrate()
        self.db.execute("INSERT OR IGNORE INTO connections VALUES('c',100,1,1)")
        self.db.execute("INSERT OR IGNORE INTO connections VALUES('d',300,3,1)")
        self.db.commit()
        self.inbox = DurableInbox(self.db,self.cipher)
        self.product = ProductState(self.db,self.routes)
        self.bot = SimpleNamespace(send_message=AsyncMock(),send_photo=AsyncMock(),process_new_updates=AsyncMock())
        ctx = dict(db=self.db,db_path=Path(path),json=json,time=time,asyncio=asyncio,types=types,
                   message_cipher=self.cipher,routes=self.routes,product=self.product,inbox=self.inbox,
                   business_tools=SimpleNamespace(repo=self.repo,service=BusinessToolsService(self.bot,self.repo,None,{}),
                          consume_self_deleted=self.repo.consume_self_deleted),
                   log=logging.getLogger('release-tests'),bot=self.bot,account_labels={},
                   mtproto_access=SimpleNamespace(owner=lambda _:False),
                   delivery_budget=DeliveryBudget(self.db),current_delivery=ContextVar('test_delivery',default=None),
                   ApiTelegramException=ApiTelegramException)
        names = {'account_for','save_message','load_message','enqueue','apply_business','dispatch_incoming',
                 'normalize_update','record_from_message','message_details','notification_target','only_others_enabled',
                 'deliver_row','outbox_worker','limited_send','DeliveryCancelled','store_connection','enqueue_html_notification'}
        nodes = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and n.name in names]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'isolated-app','exec'),ctx)
        async def recipient_for(connection):
            row=self.db.execute('SELECT account_id,enabled FROM connections WHERE connection_id=?',(connection,)).fetchone()
            return self.routes.get(row[0]) if row and row[1] else None
        ctx['recipient_for']=recipient_for
        self.ctx = ctx

    def message(self, uid=1, text='исходное синтетическое сообщение', edit=None, mid=1):
        raw = dict(message_id=mid,business_connection_id='c',date=int(time.time())-100,
                   chat=dict(id=200,type='private',first_name='Synthetic'),
                   **{'from':dict(id=200,is_bot=False,first_name='Synthetic')},text=text)
        if edit is not None:raw['edit_date']=edit
        return dict(update_id=uid,**{'edited_business_message' if edit is not None else 'business_message':raw})

    def delete(self, uid=3, mid=1):
        return dict(update_id=uid,deleted_business_messages=dict(business_connection_id='c',
                    chat=dict(id=200,type='private',first_name='Synthetic'),message_ids=[mid]))

    async def feed(self, raw):
        self.inbox.persist([raw],self.ctx['normalize_update'])
        row=self.inbox.next()
        if row:
            await self.ctx['dispatch_incoming'](row['update_id'],self.inbox.payload(row))

    def close(self):
        self.db.close()
