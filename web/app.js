'use strict';

(() => {
  const $ = (selector) => document.querySelector(selector);
  const conversation = $('#conversation');
  const form = $('#message-form');
  const input = $('#message-input');
  const keyboard = $('#request-keyboard');
  const errorBox = $('#chat-error');
  const connection = $('#connection-status');
  const state = { actor: 'client', actors: new Map(), busy: false, online: false, history: [] };
  const fallbackNames = { client: 'Клиент А', other: 'Клиент Б', staff: 'Сотрудник' };

  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function actorName() {
    return state.actors.get(state.actor)?.name || fallbackNames[state.actor];
  }

  function connectionState(online) {
    state.online = online;
    connection.classList.toggle('offline', !online);
    connection.replaceChildren(el('i'), document.createTextNode(online ? 'Локальный стенд · без Telegram' : 'Локальный стенд недоступен'));
    $('#bot-state').textContent = online ? 'Локальный бот · готов к диалогу' : 'Ожидает локального запуска';
  }

  function setBusy(busy) {
    state.busy = busy;
    document.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
    input.disabled = busy;
    form.setAttribute('aria-busy', String(busy));
    if (busy) $('#bot-state').textContent = 'Обрабатывает локальное сообщение…';
    else $('#bot-state').textContent = state.online ? 'Локальный бот · готов к диалогу' : 'Ожидает локального запуска';
  }

  function clearError() {
    errorBox.hidden = true;
    errorBox.replaceChildren();
  }

  function showError(message, retry = true) {
    errorBox.hidden = false;
    errorBox.textContent = message;
    if (retry) {
      const button = el('button', 'error-retry', 'Обновить историю');
      button.type = 'button';
      button.addEventListener('click', async () => {
        if (state.busy) return;
        setBusy(true);
        try {
          await refresh();
          clearError();
          connectionState(true);
        } catch (_) {
          connectionState(false);
          showError('Не удалось связаться со стендом. Проверьте, запущен ли локальный сервер.');
        } finally {
          setBusy(false);
        }
      });
      errorBox.append(document.createTextNode(' '), button);
    }
  }

  async function api(path, payload) {
    if (!['http:', 'https:'].includes(location.protocol)) {
      throw new Error('Для демонстрации запустите локальный сервер. Открытие HTML-файла напрямую не подключает бота.');
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {
        method: payload === undefined ? 'GET' : 'POST',
        headers: payload === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: payload === undefined ? undefined : JSON.stringify(payload),
        cache: 'no-store',
        credentials: 'same-origin',
        signal: controller.signal,
      });
      if (!(response.headers.get('content-type') || '').includes('application/json')) {
        throw new Error('Этот адрес не подключён к локальному боту. Запустите сервер по инструкции проекта.');
      }
      const data = await response.json();
      if (!response.ok) {
        throw new Error(typeof data.error === 'string' ? data.error : 'Стенд не смог обработать сообщение.');
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Стенд долго не отвечает. Обновите историю перед повторной отправкой.');
      if (error instanceof TypeError) throw new Error('Локальный сервер недоступен. Проверьте запуск и обновите историю.');
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function photoNode() {
    return $('#photo-template').content.cloneNode(true);
  }

  function addInlineButtons(container, markup) {
    if (!Array.isArray(markup?.inline_keyboard)) return;
    const holder = el('div', 'inline-keyboard');
    markup.inline_keyboard.forEach((row) => {
      if (!Array.isArray(row)) return;
      const line = el('div', 'keyboard-row');
      row.forEach((item) => {
        if (!item || typeof item.callback_data !== 'string' || typeof item.text !== 'string') return;
        const button = el('button', '', item.text);
        button.type = 'button';
        button.disabled = state.busy;
        button.addEventListener('click', () => send({ callback_data: item.callback_data, label: item.text }));
        line.append(button);
      });
      if (line.childElementCount) holder.append(line);
    });
    if (holder.childElementCount) container.append(holder);
  }

  function renderReplyKeyboard(markup) {
    keyboard.replaceChildren();
    keyboard.hidden = true;
    if (!Array.isArray(markup?.keyboard)) return;
    markup.keyboard.forEach((row) => {
      if (!Array.isArray(row)) return;
      row.forEach((item) => {
        if (typeof item === 'string') item = { text: item };
        if (!item || typeof item.text !== 'string') return;
        const isContact = item.request_contact === true;
        const button = el('button', '', isContact ? 'Передать учебный контакт' : item.text);
        button.type = 'button';
        button.disabled = state.busy;
        if (isContact) button.setAttribute('aria-label', 'Передать учебный контакт с моего согласия');
        button.addEventListener('click', () => send(isContact ? { contact: true } : { text: item.text }));
        keyboard.append(button);
      });
    });
    keyboard.hidden = keyboard.childElementCount === 0;
  }

  function renderEmpty() {
    const empty = el('div', 'empty-state');
    empty.append(el('span', 'empty-symbol', '↗'), el('h3', '', 'Здесь начинается диалог'), el('p', '', 'Нажмите «Начать диалог», чтобы открыть меню бота для выбранной роли.'));
    const start = el('button', '', 'Начать диалог');
    start.type = 'button';
    start.addEventListener('click', () => send({ text: '/start' }));
    empty.append(start);
    conversation.append(empty);
  }

  function renderHistory(messages) {
    state.history = Array.isArray(messages) ? messages : [];
    conversation.replaceChildren();
    keyboard.replaceChildren();
    keyboard.hidden = true;
    if (!state.history.length) {
      renderEmpty();
      return;
    }
    const date = el('div', 'chat-date');
    date.append(el('span', '', 'Учебный диалог · сохраняется локально'));
    conversation.append(date);
    let replyMarkup = null;
    state.history.forEach((item) => {
      if (!item || typeof item !== 'object') return;
      const params = item.params || {};
      const incoming = item.direction === 'in';
      if (item.method === 'answerCallbackQuery') {
        if (typeof params.text === 'string' && params.text) conversation.append(el('div', 'callback-note', params.text));
        return;
      }
      const hasPhoto = item.method === 'sendPhoto' || Boolean(params.photo);
      const content = typeof params.text === 'string' ? params.text : (typeof params.caption === 'string' ? params.caption : '');
      if (!hasPhoto && !content) return;
      const message = el('div', `message${incoming ? ' incoming' : ''}`);
      message.append(el('div', 'message-label', incoming ? actorName() : 'ПРИЁМНАЯ'));
      const bubble = el('div', 'bubble');
      if (hasPhoto) bubble.append(photoNode());
      if (content) bubble.append(el('p', '', content));
      message.append(bubble);
      if (!incoming) {
        addInlineButtons(message, params.reply_markup);
        if (params.reply_markup?.remove_keyboard) replyMarkup = null;
        if (Array.isArray(params.reply_markup?.keyboard)) replyMarkup = params.reply_markup;
      }
      conversation.append(message);
    });
    renderReplyKeyboard(replyMarkup);
    conversation.scrollTop = conversation.scrollHeight;
  }

  async function refresh() {
    const result = await api(`/api/history?actor=${encodeURIComponent(state.actor)}`);
    renderHistory(Array.isArray(result) ? result : result.messages);
  }

  async function send(payload) {
    if (state.busy) return;
    clearError();
    setBusy(true);
    const shouldClearInput = typeof payload.text === 'string' && input.value.trim() === payload.text;
    try {
      await api('/api/update', { actor: state.actor, ...payload });
      if (shouldClearInput) input.value = '';
      await refresh();
      connectionState(true);
    } catch (error) {
      showError(error.message || 'Не удалось обработать сообщение.');
      // Refresh only: a lost HTTP response must not resend a possibly processed update.
      try { await refresh(); } catch (_) { connectionState(false); }
    } finally {
      setBusy(false);
      if (window.innerWidth > 640) input.focus({ preventScroll: true });
    }
  }

  async function switchActor(actor) {
    if (state.busy || actor === state.actor) return;
    state.actor = actor;
    clearError();
    input.value = '';
    document.querySelectorAll('[data-actor]').forEach((button) => {
      const selected = button.dataset.actor === actor;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    $('.staff-command').hidden = actor !== 'staff';
    $('#actor-caption').textContent = `${actorName()} · учебный диалог`;
    input.placeholder = actor === 'staff' ? 'Команда мастера или сообщение…' : 'Напишите сообщение…';
    setBusy(true);
    try {
      await refresh();
      connectionState(true);
    } catch (error) {
      renderHistory([]);
      showError(error.message || 'Не удалось загрузить историю.');
      connectionState(false);
    } finally {
      setBusy(false);
    }
  }

  function renderUnavailable(error) {
    connectionState(false);
    conversation.replaceChildren();
    const empty = el('div', 'empty-state');
    empty.append(el('span', 'empty-symbol', '↗'), el('h3', '', 'Запустите локальный стенд'), el('p', '', 'Этот интерфейс работает с настоящим ядром бота. Для диалога нужен локальный сервер из комплекта проекта. Токен Telegram не требуется.'));
    const instruction = el('p', '', 'Инструкция запуска находится в README проекта.');
    instruction.style.marginTop = '12px';
    empty.append(instruction);
    conversation.append(empty);
    showError(error.message || 'Локальный сервер недоступен.');
  }

  async function boot() {
    setBusy(true);
    try {
      const session = await api('/api/session');
      if (Array.isArray(session.actors)) session.actors.forEach((actor) => { state.actors.set(actor.id, actor); });
      connectionState(true);
      $('#actor-caption').textContent = `${actorName()} · учебный диалог`;
      await refresh();
      if (!state.history.length) {
        await api('/api/update', { actor: state.actor, text: '/start' });
        await refresh();
      }
    } catch (error) {
      renderUnavailable(error);
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    send({ text });
  });
  $('#photo-button').addEventListener('click', () => send({ photo: true }));
  document.querySelectorAll('[data-command]').forEach((button) => button.addEventListener('click', () => send({ text: button.dataset.command })));
  document.querySelectorAll('[data-actor]').forEach((button) => button.addEventListener('click', () => switchActor(button.dataset.actor)));
  document.querySelectorAll('[data-sample]').forEach((button) => button.addEventListener('click', () => {
    if (state.busy) return;
    input.value = button.dataset.sample;
    input.focus({ preventScroll: true });
    if (window.innerWidth <= 640) form.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }));
  boot();
})();
