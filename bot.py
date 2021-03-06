#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Created on Sat Jun 04 12:05:50 2016

@author: SSundukov
"""
import argparse
import inspect
import json
import logging
import os
import pytz
import re
import sys
import telebot
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from collections import defaultdict

from database import Database, Result

MATCHES_PER_PAGE = 8
MSK_TZ = pytz.timezone('Europe/Moscow')
UPDATE_INTERVAL = timedelta(seconds=60)
REMIND_BEFORE = timedelta(minutes=30)

BOT_USERNAME = '@delaytevashistavkibot'
RESULTS_TABLE = u'Таблица результатов [доступна по ссылке](%s).'
PRESS_BET = u'Жми /bet, чтобы сделать новую ставку или изменить существующую.'
MY_BETS = u'/mybets, чтобы посмотреть свои ставки.'
HELP_MSG = PRESS_BET + ' ' + MY_BETS + ' ' + RESULTS_TABLE
START_MSG = u'Привет, %s! Поздравляю, ты в игре!\n'
SEND_PRIVATE_MSG = u'Tcccc, не пали контору. Напиши мне личное сообщение (%s).' % BOT_USERNAME
NAVIGATION_ERROR = u'Сорян, что-то пошло не так в строке %d. Попробуй еще раз.\n' + HELP_MSG
NO_MATCHES_MSG = u'Уже не на что ставить =('
SCORE_REQUEST = u'Сколько голов забьет %s%s?'
WINNER_REQUEST = u'Кто победит по пенальти?'
TOO_LATE_MSG = u'Уже поздно ставить на этот матч. Попробуй поставить на другой.\n' + HELP_MSG
CONFIRMATION_MSG = u'Ставка %s сделана %s. Начало матча %s по Москве. Удачи, %s!\n' + HELP_MSG
NO_BETS_MSG = u'Ты еще не сделал(а) ни одной ставки. ' + PRESS_BET
RESULTS_TITLE = u'Ставки сделаны, ставок больше нет. Начинается матч %s.'
CHOOSE_MATCH_TITLE = u'Выбери матч'
LEFT_ARROW = u'\u2b05'
RIGHT_ARROW = u'\u27a1'
NOT_REGISTERED=u'Ты пока не зарегистрирован(а). Напиши пользователю @dzhioev для получения доступа.'
ALREADY_REGISTERED=u'%s (%s) уже зарегистрирован(а).'
REGISTER_SHOULD_BE_REPLY=u'Сообщение о регистрации должно быть ответом.'
REGISTER_SHOULD_BE_REPLY_TO_FORWARD=u'Сообщение о регистрации должно быть ответом на форвард.'
REGISTRATION_SUCCESS=u'%s aka %s (%s) успешно зарегистрирован.'
ERROR_MESSAGE_ABSENT=u'Этот виджет сломан, вызови /bet снова.'
CHECK_RESULTS_BUTTON=u'Посмотреть ставки'
USER_NOT_REGISTERED=u'Пользователь не зарегистрирован.'
SUCCESS=u'Успех.'
REMIND_MSG=u'Уж встреча близится, а ставочки все нет.'

def lineno():
  return inspect.currentframe().f_back.f_lineno

def utcnow():
  return pytz.utc.localize(datetime.utcnow())

def create_bot(config):
  return telebot.TeleBot(config['token'], threaded=False)

def create_match_button(match, prediction=None):
  start_time_str = match.start_time().astimezone(MSK_TZ).strftime('%d.%m %H:%M')
  label = u'%s: %s %s' % (match.short_round(), match.label(prediction, short=True),
                          start_time_str)
  return telebot.types.InlineKeyboardButton(label,
                                            callback_data='b_%s' % match.id())

def update_job(config, bot_runner, stopped_event):
  last_update = utcnow() - UPDATE_INTERVAL
  logging.info('starting update loop')
  db = Database(config)
  matches_to_notify = {m.id() for m in db.matches.getMatchesAfter(utcnow())}
  matches_to_remind = {m.id() for m in db.matches.getMatchesAfter(utcnow() + REMIND_BEFORE)}
  while not stopped_event.is_set():
    time.sleep(1)
    if utcnow() - last_update <= UPDATE_INTERVAL:
      continue
    try:
      db = Database(config)
      bot_runner.replace_db(db)
      results = db.predictions.genResults(utcnow())
      tmp_file = None
      try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
          tmp_file = f.name
          json.dump(results, f)
        os.chmod(tmp_file, 0644)
        os.rename(tmp_file, config['results_file'])
      finally:
        if tmp_file is not None and os.path.exists(tmp_file):
          os.unlink(tmp_file)
      last_update = utcnow()
      bot = create_bot(config)
      for m in db.matches.getMatchesBefore(last_update):
        if m.id() not in matches_to_notify:
          continue
        matches_to_notify.remove(m.id())
        keyboard = telebot.types.InlineKeyboardMarkup(1)
        keyboard.add(telebot.types.InlineKeyboardButton(CHECK_RESULTS_BUTTON,
                                                        url=config['results_url']))
        bot.send_message(config['group_id'], RESULTS_TITLE % m.label(), parse_mode='Markdown',
                         reply_markup=keyboard)
      for m in db.matches.getMatchesBefore(last_update + REMIND_BEFORE):
        if m.id() not in matches_to_remind:
          continue
        matches_to_remind.remove(m.id())
        for player_id in db.predictions.getMissingPlayers(m.id()):
          keyboard = telebot.types.InlineKeyboardMarkup(1)
          keyboard.add(create_match_button(m))
          bot.send_message(player_id, REMIND_MSG, parse_mode='Markdown', reply_markup=keyboard)

      logging.info('update finished')
    except:
      logging.error(traceback.format_exc())

class BotRunner(threading.Thread):
  class BotStopper(threading.Thread):
    def __init__(self, runner, stopped_event):
      super(BotRunner.BotStopper, self).__init__(name='bot_stopper')
      self.runner = runner
      self.stopped_event = stopped_event

    def run(self):
      self.stopped_event.wait()
      self.runner.stop_bot()
      self.runner.join()

  def __init__(self, config, stopped_event, exception_event):
    super(BotRunner, self).__init__(name='bot_runner')
    self.stopped_event = stopped_event
    self.exception_event = exception_event
    self.bot = create_bot(config)
    self.db_lock = threading.Lock()
    self.db = Database(config)
    self.results_url = config['results_url']

  def get_db(self):
    with self.db_lock:
      return self.db

  def replace_db(self, new_db):
    with self.db_lock:
      self.db = new_db

  def run(self):
    BotRunner.BotStopper(self, self.stopped_event).start()
    try:
      self.run_bot()
    except:
      self.exception_event.set()
      raise
    finally:
      self.stopped_event.set()

  def register_player(self, player):
    assert(not self.is_registered(player))
    return self.get_db().players.createPlayer(player.id, player.first_name, player.last_name)

  def get_player(self, player):
    assert(self.is_registered(player))
    return self.get_db().players.getPlayer(player.id)

  def is_registered(self, user):
    return self.get_db().players.isRegistered(user.id)

  def is_admin(self, user):
    return self.get_db().players.isAdmin(user.id)

  def create_matches_page(self, page, player):
    db = self.get_db()
    matches = db.matches.getMatchesAfter(utcnow())
    matches_number = len(matches)
    if matches_number == 0:
      return None
    pages_number = (matches_number - 1) / MATCHES_PER_PAGE + 1
    page = min(page, pages_number - 1)
    matches = matches[page * MATCHES_PER_PAGE:(page + 1) * MATCHES_PER_PAGE]
    keyboard = telebot.types.InlineKeyboardMarkup(1)
    predictions = defaultdict(lambda: None)
    for m, r in db.predictions.getForPlayer(player):
      predictions[m.id()] = r
    for m in matches:
      start_time_str = m.start_time().astimezone(MSK_TZ).strftime('%d.%m %H:%M')
      label = u'%s: %s %s' % (m.short_round(), m.label(predictions[m.id()], short=True),
                              start_time_str)
      button = telebot.types.InlineKeyboardButton(label,
                                                  callback_data='b_%s' % m.id())
      keyboard.add(create_match_button(m, predictions[m.id()]))
    navs = []
    if pages_number > 1:
      navs.append(
          telebot.types.InlineKeyboardButton(
              LEFT_ARROW,
              callback_data='l_%d' % ((page + pages_number - 1) % pages_number)))
      navs.append(
          telebot.types.InlineKeyboardButton(
              '%d/%d' % (page + 1, pages_number),
              callback_data='l_%d' % page))
      navs.append(
          telebot.types.InlineKeyboardButton(
              RIGHT_ARROW,
              callback_data='l_%d' % ((page + 1)  % pages_number)))
      keyboard.row(*navs)
    title = CHOOSE_MATCH_TITLE
    return (title, keyboard)

  def run_bot(self):
    bot = self.bot
    @bot.message_handler(func=lambda m: m.chat.type != 'private')
    def on_not_private(message):
      bot.send_message(message.chat.id, SEND_PRIVATE_MSG,
                       reply_to_message_id=message.message_id)

    def check_forwarded_from(message):
      if message.reply_to_message is None:
        bot.send_message(message.chat.id, REGISTER_SHOULD_BE_REPLY)
        return None
      if message.reply_to_message.forward_from is None:
        bot.send_message(message.chat.id, REGISTER_SHOULD_BE_REPLY_TO_FORWARD)
        return None
      return message.reply_to_message.forward_from

    @bot.message_handler(commands=['register'], func=lambda m: self.is_admin(m.from_user))
    def register(message):
      forward_from = check_forwarded_from(message)
      if forward_from is None:
        return
      if self.is_registered(forward_from):
        player = self.get_player(forward_from)
        bot.send_message(message.chat.id, ALREADY_REGISTERED % (player.name(), player.id()))
        return
      player = self.register_player(message.reply_to_message.forward_from)
      bot.send_message(message.chat.id, REGISTRATION_SUCCESS % (player.name(), player.short_name(),
                                                                player.id()))
      bot.send_message(player.id(), START_MSG % player.short_name() + HELP_MSG % self.results_url,
                       parse_mode='Markdown')

    def change_queen(message, is_queen):
      forward_from = check_forwarded_from(message)
      if forward_from is None:
        return
      if not self.is_registered(forward_from):
        bot.send_message(message.chat.id, USER_NOT_REGISTERED)
        return
      self.get_db().players.changeIsQueen(forward_from.id, is_queen)
      bot.send_message(message.chat.id, SUCCESS)

    @bot.message_handler(commands=['make_queen'], func=lambda m: self.is_admin(m.from_user))
    def make_queen(message):
      change_queen(message, True)

    @bot.message_handler(commands=['unmake_queen'], func=lambda m: self.is_admin(m.from_user))
    def unmake_queen(message):
      change_queen(message, False)

    @bot.message_handler(func=lambda m: not self.is_registered(m.from_user))
    def on_not_registered(message):
      bot.send_message(message.chat.id, NOT_REGISTERED)

    @bot.message_handler(commands=['bet'])
    def start_betting(message):
      player = self.get_player(message.from_user)
      page_to_send = self.create_matches_page(0, player)
      if page_to_send is None:
        bot.send_message(message.chat.id, NO_MATCHES_MSG)
        return
      title, keyboard = page_to_send
      bot.send_message(message.chat.id, title,
                       reply_markup=keyboard)

    @bot.message_handler(commands=['mybets'])
    def list_my_bets(message):
      player = self.get_player(message.from_user)
      predictions = self.get_db().predictions.getForPlayer(player)

      if len(predictions) == 0:
        bot.send_message(message.chat.id, NO_BETS_MSG)
        return

      lines = []
      for m, r in predictions:
        lines.append('%s: %s' % (m.short_round(), m.label(r, short=True)))
      lines.append(PRESS_BET + ' ' + RESULTS_TABLE % self.results_url)
      bot.send_message(message.chat.id, '\n'.join(lines), parse_mode='Markdown')

    # keep last
    @bot.message_handler(func=lambda m: True)
    def help(message):
      bot.send_message(message.chat.id, HELP_MSG % self.results_url, parse_mode='Markdown')

    # l_<page>
    # b_<match_id>
    # b_<match_id>_<goals1>
    # b_<match_id>_<goals1>_<goals2>
    # b_<match_id>_<goals1>_<goals2>_<winner>
    @bot.callback_query_handler(lambda m: True)
    def handle_query(query):
      db = self.get_db()
      if query.message is None:
        bot.answer_callback_query(callback_query_id=query.id,
                                  text=ERROR_MESSAGE_ABSENT,
                                  show_alert=True)
        return
      bot.answer_callback_query(callback_query_id=query.id)
      player = self.get_player(query.from_user)
      data = query.data or ''

      def edit_message(text, **kwargs):
        bot.edit_message_text(text, chat_id=query.message.chat.id,
                              message_id=query.message.message_id,
                              parse_mode='Markdown',
                              **kwargs)

      def on_error(line_no):
        edit_message(NAVIGATION_ERROR % (line_no, self.results_url))

      m = re.match(r'^b_([^_]*)$', data) or \
          re.match(r'^b_([^_]*)_([0-9])$', data) or \
          re.match(r'^b_([^_]*)_([0-9])_([0-9])$', data) or \
          re.match(r'^b_([^_]*)_([0-9])_([0-9])_([12])$', data)

      if m is None:
        m = re.match(r'^l_([0-9]+)$', data)
        if m is None:
          return on_error(lineno())
        page = int(m.group(1))
        page_to_send = self.create_matches_page(page, player)
        if page_to_send is None:
          return edit_message(NO_MATCHES_MSG)
        title, keyboard = page_to_send
        return edit_message(title, reply_markup=keyboard)

      match = db.matches.getMatch(int(m.group(1)))
      if match is None:
        return on_error(lineno())

      args_len = len(m.groups())
      if args_len in [1, 2]:
        def make_button(score):
          cb_data = data + '_%d' % score
          return telebot.types.InlineKeyboardButton(str(score), callback_data=cb_data)

        keyboard = telebot.types.InlineKeyboardMarkup(3)
        keyboard.row(make_button(0))\
                .row(make_button(1), make_button(2), make_button(3))\
                .row(make_button(4), make_button(5), make_button(6))\
                .row(make_button(7), make_button(8), make_button(9))
        team = match.team(0) if args_len == 1 else match.team(1)
        return edit_message(SCORE_REQUEST % (team.flag(), team.name()), reply_markup=keyboard)

      if args_len == 4:
        if not match.is_playoff():
          return on_error(lineno())
        if m.group(2) != m.group(3):
          return on_error(lineno())
        result = Result(int(m.group(2)), int(m.group(3)), int(m.group(4)))
      else:
        result = Result(int(m.group(2)), int(m.group(3)))

      if not result.winner and match.is_playoff():
        keyboard = telebot.types.InlineKeyboardMarkup(1)
        keyboard.add(telebot.types.InlineKeyboardButton(
          match.team(0).flag() + match.team(0).name(), callback_data=data + "_1"))
        keyboard.add(telebot.types.InlineKeyboardButton(
          match.team(1).flag() + match.team(1).name(), callback_data=data + "_2"))
        return edit_message(WINNER_REQUEST, reply_markup=keyboard)

      now = utcnow()
      if match.start_time() < now:
        return edit_message(TOO_LATE_MSG % self.results_url)

      logging.info('prediction player: %s match: %s result: %s time: %s' %
                       (player.id(), match.id(), str(result), now))
      db.predictions.addPrediction(player, match, result, now)
      start_time_str = match.start_time().astimezone(MSK_TZ)\
                            .strftime(u'%d.%m в %H:%M'.encode('utf-8')).decode('utf-8')
      bet_time_str = now.astimezone(MSK_TZ)\
                        .strftime(u'%d.%m в %H:%M:%S'.encode('utf-8')).decode('utf-8')
      msg = CONFIRMATION_MSG % (match.label(result) , bet_time_str, start_time_str,
                                player.short_name(), self.results_url)
      return edit_message(msg)

    bot.polling(none_stop=True, timeout=1)

  def stop_bot(self):
    self.bot.stop_polling()

def main(config, just_dump, results_date):
  if just_dump:
    db = Database(config)
    print(str(db.teams))
    print(str(db.matches))
  if results_date is not None:
    db = Database(config)
    print(json.dumps(db.predictions.genResults(results_date), indent=2, sort_keys=True))
  if just_dump or results_date is not None:
    return
  telebot.logger.setLevel(logging.INFO)
  logging.basicConfig(format=
    '%(asctime)s (%(filename)s:%(lineno)d %(threadName)s) %(levelname)s: "%(message)s"')
  logging.getLogger().setLevel(logging.INFO)
  stopped_event = threading.Event()
  exception_event = threading.Event()
  runner = BotRunner(config, stopped_event, exception_event)
  runner.start()
  threading.Thread(target=update_job, name='update', args=(config, runner, stopped_event)).start()
  try:
    while True:
      threads = [t for t in threading.enumerate() if t != threading.current_thread()]
      if len(threads) == 0:
        break
      threads[0].join(1)
  finally:
    stopped_event.set()
  if exception_event.is_set():
    return 1

def date_arg(s):
  try:
    return pytz.utc.localize(datetime.strptime(s, '%Y-%m-%d %H:%M'))
  except ValueError:
    raise argparse.ArgumentTypeError('Not a valid date: %s' % s)

if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('config')
  parser.add_argument('-d', '--dump', help='Print teams and matches and exit',
                      action='store_true')
  parser.add_argument('-r', '--results', help='Print results json and exit',
                      nargs='?', const=utcnow(), type=date_arg)
  args = parser.parse_args(sys.argv[1:])
  with open(args.config) as config_file:
    config = json.load(config_file)
  sys.exit(main(config, args.dump, args.results))
