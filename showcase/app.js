'use strict';
(() => {
  const state = { role: 'client', step: 0, contact: false };
  const $ = (selector) => document.querySelector(selector);
  const roles = { client: 'Клиент А', staff: 'Сотрудник', other: 'Клиент Б' };
  const message = (who, text, photo = false) => ({ who, text, photo });
  const scenarios = {
    client: [
      { title: 'Обращение', heading: 'Начинаем с устройства.', text: 'Клиент запускает новую заявку. Бот последовательно запрашивает сведения, поэтому описание не теряется среди свободных сообщений.', messages: () => [message('client', '/new'), message('bot', '1 / 5 · Новая заявка\nЧто нужно отремонтировать? Название и модель.'), message('client', 'Ноутбук (демо)'), message('bot', '2 / 5 · Что случилось?\nОпишите неисправность. Не пишите пароли и другие секреты.')] },
      { title: 'Описание', heading: 'Детали вместо догадок.', text: 'Устройство и описание попадут в карточку. Стоимость и сроки бот не обещает: их согласует сотрудник после диагностики.', messages: () => [message('client', 'Демонстрация: ноутбук не включается после подключения зарядки. Индикатор питания не горит.'), message('bot', '3 / 5 · Фото неисправности\nПришлите одно фото как фотографию. Уберите с кадра личные данные. Без фото: /skip.')] },
      { title: 'Фотография', heading: 'Показываем неисправность.', text: 'В этой витрине показана условная иллюстрация. В локальном стенде используется учебное вложение. В Telegram бот принимает фотографию и хранит её file_id, не скачивая файл.', messages: () => [message('client', 'Учебное фото неисправности', true), message('bot', '4 / 5 · Контакт по вашему желанию\nРазрешаете передать ваш телефон сотрудникам мастерской для связи по этой заявке? После передачи телефон временно хранится в черновике. /cancel удаляет черновик; подтверждение создаёт заявку с телефоном. Можно отказаться и следить за статусом здесь.')] },
      { title: 'Согласие', heading: 'Телефон — по желанию.', text: 'Выберите вариант примера. Передать контакт можно только после явного согласия. Бот принимает собственную карточку контакта; чужой номер и номер текстом не сохраняет. После передачи телефон временно хранится в черновике. /cancel удаляет черновик; подтверждение создаёт заявку с телефоном. Эта страница никаких контактов не запрашивает и не хранит.', consent: true, messages: () => state.contact ? [message('client', 'Согласен, передать мой телефон'), message('bot', 'Нажмите «Поделиться своим контактом». Можно передумать: /skip.'), message('client', 'Передан учебный контакт (демо)'), message('bot', 'Учебный контакт получен. В настоящем ядре телефон временно хранится в черновике. /cancel удаляет черновик. Подтверждение создаст заявку с телефоном.')] : [message('client', 'Продолжить без телефона'), message('bot', 'Данные получены. Осталось подтвердить заявку.\nТелефон не передан. Статус можно проверить в этом чате.')] },
      { title: 'Подтверждение', heading: 'Последний взгляд перед сохранением.', text: 'До подтверждения это черновик: переданный телефон временно хранится в нём. /cancel удаляет черновик. После создания заявка получает номер, начальный статус и событие в истории. Повторное нажатие старой кнопки не создаёт вторую заявку.', consent: true, messages: () => [message('bot', '5 / 5 · Проверьте заявку\n\nУстройство: Ноутбук (демо)\nНеисправность: не включается, индикатор не горит (учебный пример).\nФото: приложено\nТелефон: ' + (state.contact ? '••• 0000 (учебный)' : 'не передан')), message('client', 'Создать заявку'), message('bot', 'Заявка №1 создана. Статус: Новая.\nСотрудник увидит её в очереди мастерской.')], status: 'Учебная заявка №1 · Новая' },
      { title: 'Статус и история', heading: 'История доступна владельцу.', text: 'Клиент открывает свою карточку и историю. Менять статус может сотрудник. Переданный телефон владелец может отозвать командой /withdrawcontact НОМЕР.', messages: () => [message('client', '/history 1'), message('bot', 'История заявки №1 · учебный пример\n\nЗаявка создана · Новая\nНовая → Диагностика · сотрудник\nДиагностика → В ремонте · сотрудник\nВ ремонте → Готова к выдаче · сотрудник')], status: 'Иллюстрация истории после работы сотрудника' },
    ],
    staff: [
      { title: 'Очередь', heading: 'Рабочая очередь мастерской.', text: 'Сотрудник видит заявки всех клиентов. В реальном режиме права выдаются по Telegram ID из STAFF_IDS. Переключатель ролей существует только для демонстрации.', messages: () => [message('staff', '/staff'), message('bot', 'Очередь мастерской · учебный пример\n\n№1 · Ноутбук (демо) · Новая'), message('staff', 'Открыть заявку №1'), message('bot', 'Заявка №1\nНовая\n\nУстройство: Ноутбук (демо)\nНеисправность: не включается.\nФото: приложено')], status: 'Сотруднику доступны заявки мастерской' },
      { title: 'Диагностика', heading: 'Разрешённый следующий шаг.', text: 'Бот проверяет права сотрудника и текущий статус. Из «Новая» можно перейти к диагностике или отменить заявку. Перескочить сразу к выдаче нельзя.', messages: () => [message('staff', '/status 1 diagnostics'), message('bot', 'Заявка №1: Новая → Диагностика.'), message('bot', 'Заявка №1\nДиагностика\n\nДоступные следующие статусы:\nВ ремонте · Отменена')], status: 'Учебная заявка №1 · Диагностика' },
      { title: 'Ремонт и выдача', heading: 'Каждый переход остаётся в истории.', text: 'Сотрудник последовательно проводит заявку через ремонт, готовность и выдачу. Устаревшая кнопка не меняет статус. Из финальных состояний переходы запрещены.', messages: () => [message('staff', '/status 1 in_progress'), message('bot', 'Диагностика → В ремонте.'), message('staff', '/status 1 ready'), message('bot', 'В ремонте → Готова к выдаче.'), message('staff', '/status 1 closed'), message('bot', 'Готова к выдаче → Выдана.')], status: 'Учебная заявка №1 · Выдана' },
      { title: 'История', heading: 'Можно восстановить ход работы.', text: 'Сохраняются события создания, смены статуса и отзыва телефона. В рабочем боте история содержит время в UTC. Здесь показана сокращённая статическая иллюстрация.', messages: () => [message('staff', '/history 1'), message('bot', 'История заявки №1 · учебный пример\n\nЗаявка создана · Новая\nНовая → Диагностика · сотрудник\nДиагностика → В ремонте · сотрудник\nВ ремонте → Готова к выдаче · сотрудник\nГотова к выдаче → Выдана · сотрудник')], status: 'История сохраняется и после выдачи' },
    ],
    other: [
      { title: 'Чужая заявка', heading: 'Номер заявки не даёт доступ.', text: 'Клиент Б пытается открыть карточку клиента А. Ядро проверяет владельца и роль. Отказ одинаковый для отсутствующей и чужой заявки, включая фотографию и историю.', messages: () => [message('other', '/ticket 1'), message('bot', 'Заявка недоступна. Проверьте номер или откройте список своих заявок: /mine.'), message('other', '/history 1'), message('bot', 'Заявка недоступна. Проверьте номер или откройте список своих заявок: /mine.')], status: 'Чужие данные не показаны' },
      { title: 'Свои заявки', heading: 'У каждого — свой список.', text: 'Обычный клиент получает только свои обращения. Попытка сменить статус или открыть очередь мастерской тоже отклоняется. Реальные проверки доступа выполняет Python, а не интерфейс.', messages: () => [message('other', '/mine'), message('bot', 'Ваши заявки: пока пусто.'), message('other', '/staff'), message('bot', 'Эта команда доступна только сотрудникам.'), message('other', '/status 1 ready'), message('bot', 'Эта команда доступна только сотрудникам.')], status: 'Права сотрудника не выдаются клиенту' },
    ],
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function render() {
    const steps = scenarios[state.role];
    const current = steps[state.step];
    const list = $('#step-list');
    list.replaceChildren();
    steps.forEach((step, index) => {
      const button = element('button', index === state.step ? 'current' : '');
      button.type = 'button';
      if (index === state.step) button.setAttribute('aria-current', 'step');
      button.append(element('span', '', String(index + 1).padStart(2, '0')), document.createTextNode(step.title));
      button.addEventListener('click', () => {
        state.step = index;
        render();
        $('#step-list').children[index].focus({ preventScroll: true });
      });
      list.append(button);
    });
    document.querySelectorAll('[data-role]').forEach((button) => {
      const selected = button.dataset.role === state.role;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    document.querySelectorAll('[data-contact]').forEach((button) => {
      const selected = (button.dataset.contact === 'yes') === state.contact;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    const stage = $('#chat-stage');
    stage.replaceChildren(element('div', 'demo-badge', roles[state.role] + ' · учебный пример'));
    current.messages().forEach((item) => {
      const incoming = item.who !== 'bot';
      const row = element('div', 'message' + (incoming ? ' client' : ''));
      row.append(element('div', 'message-label', incoming ? roles[item.who] : 'ПРИЁМНАЯ'));
      const bubble = element('div', 'message-text');
      if (item.photo) bubble.append($('#sample-photo').content.cloneNode(true));
      if (item.text) bubble.append(document.createTextNode(item.text));
      row.append(bubble);
      stage.append(row);
    });
    if (current.status) stage.append(element('div', 'state-label', current.status));
    $('#explanation-title').textContent = current.heading;
    $('#explanation-text').textContent = current.text;
    $('#contact-choice').hidden = !current.consent;
    $('#step-count').textContent = `${state.step + 1} / ${steps.length}`;
    $('#previous').disabled = state.step === 0;
    $('#next').textContent = state.step === steps.length - 1 ? 'Сначала ↺' : 'Далее →';
  }

  document.querySelectorAll('[data-role]').forEach((button) => button.addEventListener('click', () => {
    state.role = button.dataset.role;
    state.step = 0;
    render();
  }));
  document.querySelectorAll('[data-contact]').forEach((button) => button.addEventListener('click', () => {
    state.contact = button.dataset.contact === 'yes';
    render();
  }));
  $('#previous').addEventListener('click', () => {
    state.step = Math.max(0, state.step - 1);
    render();
  });
  $('#next').addEventListener('click', () => {
    state.step = (state.step + 1) % scenarios[state.role].length;
    render();
  });
  render();
})();
