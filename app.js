(function(){
  const DAY = 86400000;
  const API_BASE = 'https://api.my-berezhok-bot.net.ru';
  const CHANNEL_URL = 'https://t.me/my_berezhok';
  const CURRENCIES = [
    ['₽','₽ рубль'], ['$','$ доллар'], ['€','€ евро'], ['£','£ фунт'],
    ['₾','₾ лари'], ['֏','֏ драм'], ['₺','₺ лира'], ['₪','₪ шекель'],
    ['₸','₸ тенге'], ['₴','₴ гривна'], ['сом','сом кыргызский'],
    ['сум','сум узбекский'], ['дин','дин сербский'], ['AED','AED дирхам'],
    ['Br','Br бел. рубль'], ['¥','¥ юань']
  ];

  let items = [];
  let settings = {
    defaultWaitDays: 7,
    hideWaiting: false,
    archiveAction: 'archive',
    archiveAfterDays: 30,
    selfPronoun: 'she'
  };
  let archiveOpen = false;
  let searchQuery = '';
  let selectedPhoto = null;
  let previewObjectUrl = null;
  let detailItemId = null;
  let detailSelectedPhoto = null;
  let detailPreviewObjectUrl = null;
  let detailRemovePhoto = false;
  let pendingSnoozeId = null;
  let refreshPromise = null;
  let lastRefreshAt = 0;
  const photoObjectUrls = new Map();

  const $ = (id) => document.getElementById(id);
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const initData = tg ? tg.initData : '';
  let stateChannel = null;
  try{
    if(typeof BroadcastChannel === 'function'){
      stateChannel = new BroadcastChannel('berezhok-state');
      stateChannel.addEventListener('message', refreshWhenActive);
    }
    window.addEventListener('storage', event=>{
      if(event.key === 'berezhok-state-changed') refreshWhenActive();
    });
  }catch(error){}

  function say(feminine, masculine){
    return settings.selfPronoun === 'he' ? masculine : feminine;
  }

  function showToast(message, actionText, actionHandler, duration){
    const toast = $('toast');
    const action = $('toastAction');
    $('toastText').textContent = message;
    action.hidden = !actionText;
    action.textContent = actionText || '';
    action.onclick = actionHandler || null;
    toast.classList.add('show');
    clearTimeout(showToast._handle);
    showToast._handle = setTimeout(()=>toast.classList.remove('show'), duration || (actionText ? 6500 : 2200));
  }

  async function api(path, options){
    const isForm = options && options.body instanceof FormData;
    const headers = { 'X-Telegram-Init-Data': initData };
    if(!isForm) headers['Content-Type'] = 'application/json';
    const requestOptions = Object.assign({
      headers: Object.assign(headers, (options && options.headers) || {})
    }, options);
    delete requestOptions.silent;
    const response = await fetch(API_BASE + path, requestOptions);
    if(!response.ok) throw new Error('API error ' + response.status);
    const result = response.status === 204 ? null : await response.json();
    const method = ((options && options.method) || 'GET').toUpperCase();
    if(method !== 'GET' && !(options && options.silent)) announceStateChange();
    return result;
  }

  function announceStateChange(){
    try{
      if(stateChannel) stateChannel.postMessage('changed');
      localStorage.setItem('berezhok-state-changed', String(Date.now()));
    }catch(error){}
  }

  async function loadData(){
    try{
      const state = await api('/api/state');
      items = state.items;
      settings = Object.assign(settings, state.settings);
      // Карточки должны появиться сразу после получения состояния. Медленная
      // или зависшая загрузка одной фотографии не должна скрывать весь список.
      loadPhotoUrls().then(render).catch(()=>{});
      return true;
    }catch(error){
      showToast('Не удалось загрузить данные — проверь связь с сервером');
      return false;
    }
  }

  async function refreshWhenActive(){
    const now = Date.now();
    if(refreshPromise || now - lastRefreshAt < 1000) return refreshPromise;
    lastRefreshAt = now;
    refreshPromise = (async ()=>{
      const loaded = await loadData();
      if(loaded) render();
      await openPendingLinkIfAny();
    })();
    try{
      await refreshPromise;
    }finally{
      refreshPromise = null;
    }
  }

  async function loadPhotoUrls(){
    const wanted = new Set(items.filter(item=>item.hasPhoto).map(item=>item.id));
    for(const [id, objectUrl] of photoObjectUrls){
      if(!wanted.has(id)){
        URL.revokeObjectURL(objectUrl);
        photoObjectUrls.delete(id);
      }
    }
    await Promise.all(items.filter(item=>item.hasPhoto && !photoObjectUrls.has(item.id)).map(async item=>{
      try{
        const response = await fetch(`${API_BASE}/api/items/${item.id}/photo`, {
          headers: { 'X-Telegram-Init-Data': initData }
        });
        if(!response.ok) return;
        photoObjectUrls.set(item.id, URL.createObjectURL(await response.blob()));
      }catch(error){}
    }));
  }

  function invalidatePhoto(id){
    const objectUrl = photoObjectUrls.get(id);
    if(objectUrl) URL.revokeObjectURL(objectUrl);
    photoObjectUrls.delete(id);
  }

  function createItemOnServer(payload, photo){
    const form = new FormData();
    form.append('name', payload.name);
    form.append('url', payload.url);
    form.append('price', payload.price);
    form.append('reason', payload.reason);
    if(payload.waitDays !== null) form.append('waitDays', String(payload.waitDays));
    if(photo) form.append('photo', photo, 'photo.jpg');
    return api('/api/items-with-photo', { method: 'POST', body: form });
  }

  function updateItemOnServer(id, payload, photo, removePhoto){
    const form = new FormData();
    form.append('name', payload.name);
    form.append('url', payload.url);
    form.append('price', payload.price);
    form.append('reason', payload.reason);
    form.append('waitDays', payload.waitDays === null ? 'default' : String(payload.waitDays));
    form.append('removePhoto', removePhoto ? 'true' : 'false');
    if(photo) form.append('photo', photo, 'photo.jpg');
    return api(`/api/items/${id}`, { method: 'PUT', body: form });
  }

  function deleteItemOnServer(id){
    return api(`/api/items/${id}`, { method: 'DELETE' });
  }

  function decideOnServer(id, decision){
    return api(`/api/items/${id}/decide`, {
      method: 'POST', body: JSON.stringify({ decision })
    });
  }

  function snoozeOnServer(id, days){
    return api(`/api/items/${id}/snooze`, {
      method: 'POST', body: JSON.stringify({ days })
    });
  }

  function undoOnServer(id){
    return api(`/api/items/${id}/undo`, { method: 'POST' });
  }

  function restoreOnServer(id){
    return api(`/api/items/${id}/restore`, { method: 'POST' });
  }

  function consumePendingLink(){
    return api('/api/pending-link/consume', { method: 'POST', silent: true });
  }

  async function openPendingLinkIfAny(){
    try{
      const pending = await consumePendingLink();
      if(pending && pending.url){
        $('addForm').reset();
        clearSelectedPhoto();
        openAdd(pending.url);
        showToast('Ссылка уже в карточке — осталось добавить название');
      }
    }catch(error){}
  }

  async function saveSettings(partial){
    try{
      await api('/api/settings', { method: 'PUT', body: JSON.stringify(partial) });
    }catch(error){
      showToast('Не получилось сохранить настройки');
    }
  }

  function escHTML(value){
    return String(value || '').replace(/[&<>"']/g, char=>({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[char]));
  }

  function escAttr(value){ return escHTML(value); }

  function fmtDays(number){
    const absolute = Math.abs(number);
    const remainder = absolute % 10;
    const hundred = absolute % 100;
    let word = 'дней';
    if(hundred < 11 || hundred > 14){
      if(remainder === 1) word = 'день';
      else if(remainder >= 2 && remainder <= 4) word = 'дня';
    }
    return absolute + ' ' + word;
  }

  function fmtRemaining(milliseconds){
    if(milliseconds < 3600000) return Math.max(1, Math.round(milliseconds / 60000)) + ' мин';
    if(milliseconds < DAY) return Math.max(1, Math.round(milliseconds / 3600000)) + ' ч';
    return fmtDays(Math.ceil(milliseconds / DAY));
  }

  function fmtWaitDays(days){
    return days < 1 ? fmtRemaining(days * DAY) : fmtDays(days);
  }

  function itemStatus(item){
    if(item.archived || item.decision) return 'archived';
    const wait = (item.waitDays ?? settings.defaultWaitDays) * DAY;
    return Date.now() >= item.addedAt + wait ? 'ready' : 'waiting';
  }

  function thumbHTML(item){
    const photoUrl = photoObjectUrls.get(item.id);
    if(photoUrl) return `<div class="thumb"><img src="${escAttr(photoUrl)}" alt=""></div>`;
    return `<div class="thumb">${escHTML((item.name || '?').charAt(0).toUpperCase())}</div>`;
  }

  function itemNameHTML(item){
    return item.url
      ? `<a class="card-name" href="${escAttr(item.url)}" target="_blank" rel="noopener">${escHTML(item.name)}</a>`
      : `<span class="card-name">${escHTML(item.name)}</span>`;
  }

  function renderReadyCard(item){
    return `
      <div class="card clickable" data-id="${item.id}" tabindex="0" role="button" aria-label="Открыть ${escAttr(item.name)}">
        <div class="card-top">
          ${thumbHTML(item)}
          <div class="card-info">
            ${itemNameHTML(item)}
            ${item.price ? `<div class="card-price">${escHTML(displayPrice(item.price))}</div>` : ''}
          </div>
        </div>
        ${item.reason ? `<div class="card-reason">«${escHTML(item.reason)}»</div>` : ''}
        <div class="gauge-row">
          <div class="gauge"><div class="gauge-fill is-ready" style="width:100%"></div></div>
          <span class="gauge-label">пора решать</span>
        </div>
        <div class="actions ready-actions">
          <button class="btn bought" data-action="bought" data-id="${item.id}">${say('Купила','Купил')}</button>
          <button class="btn drop" data-action="drop" data-id="${item.id}">Уже не надо</button>
          <button class="btn" data-action="keep" data-id="${item.id}">В желания</button>
          <button class="btn" data-action="snooze" data-id="${item.id}">Подождать ещё</button>
        </div>
      </div>`;
  }

  function renderWaitingCard(item){
    const wait = item.waitDays ?? settings.defaultWaitDays;
    const waitMilliseconds = wait * DAY;
    const elapsed = Date.now() - item.addedAt;
    const percent = Math.max(3, Math.min(100, Math.round((elapsed / waitMilliseconds) * 100)));
    const left = Math.max(0, waitMilliseconds - elapsed);
    return `
      <div class="card waiting-card clickable" data-id="${item.id}" tabindex="0" role="button" aria-label="Открыть ${escAttr(item.name)}">
        <div class="card-top">
          ${thumbHTML(item)}
          <div class="card-info">
            ${itemNameHTML(item)}
            ${item.price ? `<div class="card-price">${escHTML(displayPrice(item.price))}</div>` : ''}
          </div>
        </div>
        <div class="gauge-row">
          <div class="gauge"><div class="gauge-fill" style="width:${percent}%"></div></div>
          <span class="gauge-label">ещё ${fmtRemaining(left)}</span>
        </div>
      </div>`;
  }

  function renderArchiveRow(item){
    let badge;
    if(item.decision === 'bought') badge = `<span class="badge kept">${say('купила','купил')}</span>`;
    else if(item.decision === 'keep') badge = `<span class="badge kept">${say('оставила в желаниях','оставил в желаниях')}</span>`;
    else if(item.decision === 'expired') badge = '<span class="badge expired">истёк срок</span>';
    else badge = `<span class="badge dropped">${say('отказалась','отказался')}</span>`;
    return `
      <div class="card clickable" data-id="${item.id}" tabindex="0" role="button" aria-label="Открыть ${escAttr(item.name)}" style="opacity:0.85;">
        <div class="card-top">
          ${thumbHTML(item)}
          <div class="card-info">
            ${itemNameHTML(item)}
            ${item.price ? `<div class="card-price">${escHTML(displayPrice(item.price))}</div>` : ''}
            ${badge}
          </div>
        </div>
      </div>`;
  }

  function parsePrice(price){
    const text = String(price || '').trim();
    if(!text) return null;
    const tokens = CURRENCIES.map(entry=>entry[0]).sort((a,b)=>b.length-a.length);
    let currency = null;
    let numberText = text;
    for(const token of tokens){
      if(text.startsWith(token)){
        currency = token;
        numberText = text.slice(token.length);
        break;
      }
      if(text.endsWith(token)){
        currency = token;
        numberText = text.slice(0, -token.length);
        break;
      }
    }
    if(!numberText.trim()) return null;
    const amount = Number(numberText.replace(/\s/g, '').replace(',', '.'));
    if(!Number.isFinite(amount)) return null;
    if(!currency) currency = '₽';
    return { amount, currency };
  }

  function formatAmount(amount, currency){
    const number = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(amount);
    return currency === '$' || currency === '£' ? currency + number : number + ' ' + currency;
  }

  function displayPrice(price){
    const parsed = parsePrice(price);
    return parsed ? formatAmount(parsed.amount, parsed.currency) : String(price || '');
  }

  function renderEffect(){
    const dropped = items.filter(item=>item.decision === 'drop');
    const card = $('effectCard');
    if(!dropped.length){
      card.classList.remove('visible');
      return;
    }
    const totals = new Map();
    dropped.forEach(item=>{
      const parsed = parsePrice(item.price);
      if(parsed) totals.set(parsed.currency, (totals.get(parsed.currency) || 0) + parsed.amount);
    });
    const count = dropped.length;
    $('effectTitle').textContent = say(
      `Ты отказалась от ${count} ${count === 1 ? 'покупки' : 'покупок'}`,
      `Ты отказался от ${count} ${count === 1 ? 'покупки' : 'покупок'}`
    );
    const amounts = Array.from(totals, ([currency, amount])=>formatAmount(amount, currency));
    $('effectText').textContent = amounts.length
      ? `${say('Сохранила','Сохранил')}: ${amounts.join(' · ')}`
      : 'Добавляй цену — здесь появится сохранённая сумма.';
    card.classList.add('visible');
  }

  function renderMonthly(){
    const now = new Date();
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
    const decided = items.filter(item=>item.decidedAt && item.decidedAt >= monthStart);
    const bought = decided.filter(item=>item.decision === 'bought').length;
    const dropped = decided.filter(item=>item.decision === 'drop');
    const kept = decided.filter(item=>item.decision === 'keep').length;
    const month = new Intl.DateTimeFormat('ru-RU', { month: 'long' }).format(now);
    $('monthlyTitle').textContent = `Итог за ${month}`;
    if(!decided.length){
      $('monthlyText').textContent = 'В этом месяце решений пока нет.';
    }else{
      const totals = new Map();
      dropped.forEach(item=>{
        const parsed = parsePrice(item.price);
        if(parsed) totals.set(parsed.currency, (totals.get(parsed.currency) || 0) + parsed.amount);
      });
      const saved = Array.from(totals, ([currency, amount])=>formatAmount(amount, currency));
      const parts = [`куплено: ${bought}`, `не понадобилось: ${dropped.length}`, `в желаниях: ${kept}`];
      if(saved.length) parts.push(`сохранено: ${saved.join(' · ')}`);
      $('monthlyText').textContent = parts.join(' · ');
    }
    $('monthlyCard').classList.add('visible');
  }

  function matchesSearch(item){
    if(!searchQuery) return true;
    return [item.name, item.price, item.url, item.reason].join(' ').toLocaleLowerCase('ru').includes(searchQuery);
  }

  function bindCards(){
    document.querySelectorAll('.card.clickable').forEach(card=>{
      card.addEventListener('click', event=>{
        if(event.target.closest('button,a')) return;
        openDetail(card.dataset.id);
      });
      card.addEventListener('keydown', event=>{
        if(event.key === 'Enter' || event.key === ' '){
          event.preventDefault();
          openDetail(card.dataset.id);
        }
      });
    });
    document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click', onCardAction));
  }

  function render(){
    $('archiveDecisionLabel').textContent = say(
      'Если не приняла решение за месяц',
      'Если не принял решение за месяц'
    );
    renderEffect();
    renderMonthly();
    const filtered = items.filter(matchesSearch);
    const ready = filtered.filter(item=>itemStatus(item) === 'ready');
    const waiting = filtered.filter(item=>itemStatus(item) === 'waiting');
    const archived = filtered.filter(item=>itemStatus(item) === 'archived');
    const searching = Boolean(searchQuery);

    $('searchClear').classList.toggle('visible', searching);
    $('searchEmpty').classList.toggle('visible', searching && filtered.length === 0);
    $('readyCount').textContent = ready.length;
    $('waitingCount').textContent = waiting.length;
    $('archiveCount').textContent = archived.length;

    $('readySection').style.display = searching && !ready.length ? 'none' : 'block';
    $('readyList').innerHTML = ready.length
      ? ready.map(renderReadyCard).join('')
      : '<div class="empty">Пока нечего решать. Как только что-то отлежится — появится здесь.</div>';

    if(searching){
      $('waitingSection').style.display = waiting.length ? 'block' : 'none';
      $('waitingList').innerHTML = waiting.map(renderWaitingCard).join('');
    }else if(settings.hideWaiting){
      $('waitingSection').style.display = waiting.length ? 'block' : 'none';
      if(waiting.length){
        $('waitingList').innerHTML = `<div class="peek" id="peekWaiting">Кое-что ещё «остывает» (${waiting.length}) — посмотреть</div>`;
        $('peekWaiting').addEventListener('click', ()=>{
          $('waitingList').innerHTML = waiting.map(renderWaitingCard).join('');
          bindCards();
        });
      }
    }else{
      $('waitingSection').style.display = 'block';
      $('waitingList').innerHTML = waiting.length
        ? waiting.map(renderWaitingCard).join('')
        : '<div class="empty">Список ожидания пуст. Добавь то, на что смотришь прямо сейчас — вернёмся к этому позже.</div>';
    }

    $('archiveSection').style.display = searching && !archived.length ? 'none' : 'block';
    $('archiveList').innerHTML = archived.length
      ? archived.slice().reverse().map(renderArchiveRow).join('')
      : '<div class="empty">Архив пуст.</div>';
    const showArchive = searching ? archived.length > 0 : archiveOpen;
    $('archiveList').style.display = showArchive ? 'block' : 'none';
    $('archiveToggle').disabled = searching;
    $('archiveToggle').innerHTML = searching
      ? `Архив (<span id="archiveCount">${archived.length}</span>)`
      : `Архив (<span id="archiveCount">${archived.length}</span>) — ${showArchive ? 'скрыть' : 'показать'}`;
    bindCards();
  }

  function decisionToast(action){
    if(action === 'bought') return say('Купила — пусть радует!','Купил — пусть радует!');
    if(action === 'keep') return say('Оставила в желаниях','Оставил в желаниях');
    return say('Убрала из списка','Убрал из списка');
  }

  async function undoLastAction(id){
    try{
      await undoOnServer(id);
      await loadData();
      render();
      showToast('Действие отменено');
    }catch(error){
      showToast('Не получилось отменить действие');
    }
  }

  async function onCardAction(event){
    event.stopPropagation();
    const id = event.currentTarget.dataset.id;
    const action = event.currentTarget.dataset.action;
    if(action === 'snooze') return openSnooze(id);
    try{
      await decideOnServer(id, action);
      closeDetail();
      await loadData();
      render();
      showToast(decisionToast(action), 'Отменить', ()=>undoLastAction(id));
    }catch(error){
      showToast('Не получилось сохранить решение');
    }
  }

  function statusText(item){
    const status = itemStatus(item);
    if(status === 'ready') return 'Пора принять решение';
    if(status === 'waiting') return 'Вещь ещё ждёт своего часа';
    if(item.decision === 'bought') return say('Купила','Купил');
    if(item.decision === 'keep') return say('Оставила в желаниях','Оставил в желаниях');
    if(item.decision === 'drop') return say('Отказалась от покупки','Отказался от покупки');
    return 'Перенесено в архив';
  }

  function renderDetailPhoto(item){
    const container = $('detailPhoto');
    if(detailPreviewObjectUrl){
      container.innerHTML = `<img src="${escAttr(detailPreviewObjectUrl)}" alt="Фото вещи">`;
    }else if(!detailRemovePhoto && photoObjectUrls.get(item.id)){
      container.innerHTML = `<img src="${escAttr(photoObjectUrls.get(item.id))}" alt="Фото вещи">`;
    }else{
      container.textContent = (item.name || '?').charAt(0).toUpperCase();
    }
    $('detailPhotoRemove').hidden = !detailSelectedPhoto && (detailRemovePhoto || !item.hasPhoto);
  }

  function openDetail(id){
    const item = items.find(candidate=>candidate.id === id);
    if(!item) return;
    detailItemId = id;
    detailSelectedPhoto = null;
    detailRemovePhoto = false;
    if(detailPreviewObjectUrl) URL.revokeObjectURL(detailPreviewObjectUrl);
    detailPreviewObjectUrl = null;
    $('d-photo').value = '';
    $('d-name').value = item.name || '';
    $('d-url').value = item.url || '';
    $('d-reason').value = item.reason || '';
    const parsed = parsePrice(item.price);
    $('d-price').value = parsed ? parsed.amount : '';
    $('d-currency').value = parsed ? parsed.currency : '₽';
    $('d-wait').querySelectorAll('[data-custom-wait]').forEach(option=>option.remove());
    const waitValue = item.waitDays == null ? 'default' : String(item.waitDays);
    if(!Array.from($('d-wait').options).some(option=>option.value === waitValue)){
      const custom = new Option(`${item.waitDays} дн. (текущий срок)`, waitValue);
      custom.dataset.customWait = 'true';
      $('d-wait').add(custom);
    }
    $('d-wait').value = waitValue;
    $('detailStatus').textContent = statusText(item);
    const archived = itemStatus(item) === 'archived';
    $('detailActions').style.display = itemStatus(item) === 'ready' ? 'grid' : 'none';
    $('restoreItem').hidden = !archived;
    document.querySelector('[data-detail-action="bought"]').textContent = say('Купила','Купил');
    renderDetailPhoto(item);
    $('detailOverlay').classList.add('open');
  }

  function closeDetail(){
    $('detailOverlay').classList.remove('open');
    if(detailPreviewObjectUrl) URL.revokeObjectURL(detailPreviewObjectUrl);
    detailPreviewObjectUrl = null;
    detailSelectedPhoto = null;
    detailItemId = null;
  }

  function buildPrice(amount, currency){
    const value = String(amount || '').trim();
    if(!value) return '';
    return currency === '$' || currency === '£' ? currency + value : value + ' ' + currency;
  }

  function clearSelectedPhoto(){
    selectedPhoto = null;
    $('f-photo').value = '';
    if(previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = null;
    $('photoPreviewImage').removeAttribute('src');
    $('photoPreview').classList.remove('visible');
    $('photoAddLabel').hidden = false;
  }

  function validatePhoto(file){
    if(file.type && !file.type.startsWith('image/')){
      showToast('Нужен файл с фотографией');
      return false;
    }
    if(file.size > 20 * 1024 * 1024){
      showToast('Фото слишком большое — выбери до 20 МБ');
      return false;
    }
    return true;
  }

  async function compressPhoto(file){
    const objectUrl = URL.createObjectURL(file);
    try{
      const image = new Image();
      await new Promise((resolve, reject)=>{
        image.onload = resolve;
        image.onerror = reject;
        image.src = objectUrl;
      });
      const scale = Math.min(1, 1600 / Math.max(image.naturalWidth, image.naturalHeight));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
      return await new Promise((resolve, reject)=>{
        canvas.toBlob(blob=>blob ? resolve(blob) : reject(new Error('image conversion failed')), 'image/jpeg', .84);
      });
    }finally{
      URL.revokeObjectURL(objectUrl);
    }
  }

  function openSnooze(id){
    pendingSnoozeId = id;
    $('snoozeOverlay').classList.add('open');
  }

  function closeSnooze(){
    $('snoozeOverlay').classList.remove('open');
    pendingSnoozeId = null;
  }

  function askConfirm(message){
    if(tg && typeof tg.showConfirm === 'function'){
      return new Promise(resolve=>tg.showConfirm(message, resolve));
    }
    return Promise.resolve(window.confirm(message));
  }

  function openAdd(url){
    if(url) $('f-url').value = url;
    $('addOverlay').classList.add('open');
  }

  $('openAddBtn').addEventListener('click', ()=>openAdd());
  $('closeAdd').addEventListener('click', ()=>$('addOverlay').classList.remove('open'));
  $('addOverlay').addEventListener('click', event=>{
    if(event.target.id === 'addOverlay') $('addOverlay').classList.remove('open');
  });

  $('f-photo').addEventListener('change', ()=>{
    const file = $('f-photo').files && $('f-photo').files[0];
    if(!file) return clearSelectedPhoto();
    if(!validatePhoto(file)) return clearSelectedPhoto();
    clearSelectedPhoto();
    selectedPhoto = file;
    previewObjectUrl = URL.createObjectURL(file);
    $('photoPreviewImage').src = previewObjectUrl;
    $('photoPreviewName').textContent = file.name || 'Выбранное фото';
    $('photoPreview').classList.add('visible');
    $('photoAddLabel').hidden = true;
  });
  $('photoRemove').addEventListener('click', clearSelectedPhoto);

  $('addForm').addEventListener('submit', async event=>{
    event.preventDefault();
    const name = $('f-name').value.trim();
    if(!name) return;
    const waitValue = $('f-wait').value;
    const payload = {
      name,
      url: $('f-url').value.trim(),
      price: buildPrice($('f-price').value, $('f-currency').value),
      reason: $('f-reason').value.trim(),
      waitDays: waitValue === 'default' ? null : parseFloat(waitValue)
    };
    const submit = event.currentTarget.querySelector('[type="submit"]');
    submit.disabled = true;
    submit.textContent = selectedPhoto ? 'Готовим фотографию…' : 'Сохраняем…';
    try{
      const photo = selectedPhoto ? await compressPhoto(selectedPhoto) : null;
      submit.textContent = 'Сохраняем…';
      await createItemOnServer(payload, photo);
      $('addForm').reset();
      clearSelectedPhoto();
      $('addOverlay').classList.remove('open');
      await loadData();
      render();
      showToast(say('Положила в лист ожидания','Положил в лист ожидания'));
    }catch(error){
      showToast('Не получилось сохранить вещь или фото');
    }finally{
      submit.disabled = false;
      submit.textContent = 'Положить в лист ожидания';
    }
  });

  $('closeDetail').addEventListener('click', closeDetail);
  $('detailOverlay').addEventListener('click', event=>{
    if(event.target.id === 'detailOverlay') closeDetail();
  });
  $('d-photo').addEventListener('change', ()=>{
    const file = $('d-photo').files && $('d-photo').files[0];
    if(!file || !validatePhoto(file)) return;
    if(detailPreviewObjectUrl) URL.revokeObjectURL(detailPreviewObjectUrl);
    detailSelectedPhoto = file;
    detailRemovePhoto = false;
    detailPreviewObjectUrl = URL.createObjectURL(file);
    const item = items.find(candidate=>candidate.id === detailItemId);
    if(item) renderDetailPhoto(item);
  });
  $('detailPhotoRemove').addEventListener('click', ()=>{
    if(detailPreviewObjectUrl) URL.revokeObjectURL(detailPreviewObjectUrl);
    detailPreviewObjectUrl = null;
    detailSelectedPhoto = null;
    detailRemovePhoto = true;
    $('d-photo').value = '';
    const item = items.find(candidate=>candidate.id === detailItemId);
    if(item) renderDetailPhoto(item);
  });
  $('detailForm').addEventListener('submit', async event=>{
    event.preventDefault();
    const id = detailItemId;
    if(!id) return;
    const waitValue = $('d-wait').value;
    const payload = {
      name: $('d-name').value.trim(),
      url: $('d-url').value.trim(),
      price: buildPrice($('d-price').value, $('d-currency').value),
      reason: $('d-reason').value.trim(),
      waitDays: waitValue === 'default' ? null : parseFloat(waitValue)
    };
    const submit = event.currentTarget.querySelector('[type="submit"]');
    submit.disabled = true;
    submit.textContent = 'Сохраняем…';
    try{
      const photo = detailSelectedPhoto ? await compressPhoto(detailSelectedPhoto) : null;
      await updateItemOnServer(id, payload, photo, detailRemovePhoto);
      invalidatePhoto(id);
      closeDetail();
      await loadData();
      render();
      showToast(say('Сохранила изменения','Сохранил изменения'));
    }catch(error){
      showToast('Не получилось сохранить изменения');
    }finally{
      submit.disabled = false;
      submit.textContent = 'Сохранить изменения';
    }
  });
  $('deleteItem').addEventListener('click', async ()=>{
    const id = detailItemId;
    if(!id || !(await askConfirm('Удалить эту вещь? Действие можно будет сразу отменить.'))) return;
    try{
      await deleteItemOnServer(id);
      invalidatePhoto(id);
      closeDetail();
      await loadData();
      render();
      showToast(say('Удалила вещь','Удалил вещь'), 'Отменить', ()=>undoLastAction(id));
    }catch(error){
      showToast('Не получилось удалить вещь');
    }
  });
  $('restoreItem').addEventListener('click', async ()=>{
    const id = detailItemId;
    if(!id) return;
    try{
      await restoreOnServer(id);
      closeDetail();
      await loadData();
      render();
      showToast('Вернули вещь на подумать');
    }catch(error){
      showToast('Не получилось вернуть вещь');
    }
  });
  document.querySelectorAll('[data-detail-action]').forEach(button=>{
    button.addEventListener('click', event=>{
      const action = event.currentTarget.dataset.detailAction;
      if(action === 'snooze') return openSnooze(detailItemId);
      onCardAction({
        stopPropagation(){},
        currentTarget: { dataset: { id: detailItemId, action } }
      });
    });
  });

  $('closeSnooze').addEventListener('click', closeSnooze);
  $('snoozeOverlay').addEventListener('click', event=>{
    if(event.target.id === 'snoozeOverlay') closeSnooze();
  });
  document.querySelectorAll('[data-snooze-days]').forEach(button=>{
    button.addEventListener('click', async event=>{
      const id = pendingSnoozeId;
      const days = Number(event.currentTarget.dataset.snoozeDays);
      if(!id) return;
      try{
        await snoozeOnServer(id, days);
        closeSnooze();
        closeDetail();
        await loadData();
        render();
        showToast(`Вернёмся к этому через ${fmtWaitDays(days)}`);
      }catch(error){
        showToast('Не получилось перенести срок');
      }
    });
  });

  $('settingsBtn').addEventListener('click', ()=>{
    $('s-pronoun').value = settings.selfPronoun;
    $('s-wait').value = String(settings.defaultWaitDays);
    $('s-hideToggle').classList.toggle('on', !settings.hideWaiting);
    $('s-hideToggle').setAttribute('aria-checked', String(!settings.hideWaiting));
    $('s-archiveAction').value = settings.archiveAction;
    $('settingsOverlay').classList.add('open');
  });
  $('closeSettings').addEventListener('click', ()=>$('settingsOverlay').classList.remove('open'));
  $('settingsOverlay').addEventListener('click', event=>{
    if(event.target.id === 'settingsOverlay') $('settingsOverlay').classList.remove('open');
  });
  $('s-pronoun').addEventListener('change', async ()=>{
    settings.selfPronoun = $('s-pronoun').value;
    await saveSettings({ selfPronoun: settings.selfPronoun });
    render();
  });
  $('s-wait').addEventListener('change', async ()=>{
    settings.defaultWaitDays = parseFloat($('s-wait').value);
    await saveSettings({ defaultWaitDays: settings.defaultWaitDays });
    render();
  });
  $('s-hideToggle').addEventListener('click', async ()=>{
    const nowOn = !$('s-hideToggle').classList.contains('on');
    $('s-hideToggle').classList.toggle('on', nowOn);
    $('s-hideToggle').setAttribute('aria-checked', String(nowOn));
    settings.hideWaiting = !nowOn;
    await saveSettings({ hideWaiting: settings.hideWaiting });
    render();
  });
  $('s-archiveAction').addEventListener('change', async ()=>{
    settings.archiveAction = $('s-archiveAction').value;
    await saveSettings({ archiveAction: settings.archiveAction });
  });
  $('channelLink').addEventListener('click', event=>{
    if(tg && typeof tg.openTelegramLink === 'function'){
      event.preventDefault();
      $('settingsOverlay').classList.remove('open');
      tg.openTelegramLink(CHANNEL_URL);
    }
  });

  $('searchInput').addEventListener('input', ()=>{
    searchQuery = $('searchInput').value.trim().toLocaleLowerCase('ru');
    render();
  });
  $('searchClear').addEventListener('click', ()=>{
    $('searchInput').value = '';
    searchQuery = '';
    render();
    $('searchInput').focus();
  });
  $('archiveToggle').addEventListener('click', ()=>{
    archiveOpen = !archiveOpen;
    render();
  });

  (async function init(){
    $('d-currency').innerHTML = CURRENCIES.map(([value,label])=>`<option value="${escAttr(value)}">${escHTML(label)}</option>`).join('');
    if(tg){
      tg.ready();
      tg.expand();
      if(typeof tg.onEvent === 'function'){
        tg.onEvent('activated', refreshWhenActive);
      }
      try{ tg.setHeaderColor('#EDE9E0'); }catch(error){}
      try{ tg.setBackgroundColor('#EDE9E0'); }catch(error){}
    }
    document.addEventListener('visibilitychange', ()=>{
      if(!document.hidden) refreshWhenActive();
    });
    window.addEventListener('focus', refreshWhenActive);
    window.addEventListener('pageshow', refreshWhenActive);
    setInterval(()=>{
      if(!document.hidden) refreshWhenActive();
    }, 60000);
    await loadData();
    lastRefreshAt = Date.now();
    render();
    await openPendingLinkIfAny();
  })();
})();
