# Бережок

Telegram Mini App со списком покупок «на подумать» и ботом-напоминателем.

В текущей версии доступны фотографии, поиск, карточка с редактированием и
удалением, решения «купила/купил», «уже не надо» и «подождать ещё», а также
эффект отказов с отдельными итогами по каждой валюте. В карточке можно сохранить
причину «Почему я это хочу?», решения и мягкое удаление можно сразу отменить,
архивные вещи — вернуть на подумать. На главной показывается итог текущего месяца.

Если отправить боту сообщение со ссылкой `https://…`, он сохранит её как черновик.
При следующем открытии Бережка форма новой вещи откроется с заполненной ссылкой.

## Фотографии

Мини-приложение уменьшает выбранную фотографию до 1600 px и отправляет её API.
Сервер повторно проверяет изображение, сохраняет JPEG в `server/uploads/`, а в
SQLite хранит только внутреннее имя файла. Получение фотографии защищено
Telegram `initData`: пользователь может загрузить только фотографии своих вещей.

При первом запуске новой версии в существующую таблицу `items` добавляется колонка
`photo_filename`. Это миграция через `ALTER TABLE`; существующие записи не удаляются.

Для работы сервера после обновления зависимостей:

```bash
cd /root/berezhok/server
venv/bin/pip install -r requirements.txt
systemctl restart berezhok-api berezhok-bot
```

По умолчанию фотографии хранятся в каталоге рядом с базой. Путь можно изменить:

```env
PHOTO_DIR=/root/berezhok/server/uploads
```

В резервную копию нужно включать и `berezhok.db`, и каталог `uploads/`.

### Автоматические резервные копии

`server/backup.py` делает согласованную копию работающей SQLite-базы, добавляет
в неё фотографии и сохраняет единый архив. Таймер запускает эту операцию раз в
сутки и хранит архивы 30 дней.

Установка таймера на сервере:

```bash
mkdir -p /root/backups/berezhok
cp /root/berezhok/deploy/berezhok-backup.service /etc/systemd/system/
cp /root/berezhok/deploy/berezhok-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now berezhok-backup.timer
systemctl start berezhok-backup.service
systemctl status berezhok-backup.service --no-pager
ls -lh /root/backups/berezhok
```

Проверка расписания:

```bash
systemctl list-timers berezhok-backup.timer --no-pager
```

Проверка миграции и API фотографий:

```bash
pip install -r server/requirements-dev.txt
python -m unittest server/test_app.py -v
```
