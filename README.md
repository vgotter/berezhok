# Бережок

Telegram Mini App со списком покупок «на подумать» и ботом-напоминателем.

В текущей версии доступны фотографии, поиск, карточка с редактированием и
удалением, решения «купила/купил», «уже не надо» и «подождать ещё», а также
эффект отказов с отдельными итогами по каждой валюте. В карточке можно сохранить
причину «Почему я это хочу?», решения и мягкое удаление можно сразу отменить,
архивные вещи — вернуть на подумать. Вещи с решением «оставить в
желаниях» собираются в отдельном списке желаний, где покупку можно
отметить галочкой. На главной показывается итог текущего месяца.
В подключённой «Пультовой» создательница может вызвать команду `/stats`: она
показывает рост, активность и использование функций без содержимого карточек.

Если отправить боту сообщение со ссылкой `https://…`, он попробует извлечь из
Open Graph и Schema.org название, цену и фотографию, когда сайт доступен серверу
без региональных ограничений. Бот покажет черновик прямо в чате: название и цену
можно изменить кнопками, а после подтверждения вещь
сохранится в Бережке без открытия Mini App.

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

Проверка, что конкретный архив действительно читается, база цела, а фотографии
не повреждены:

```bash
cd /root/berezhok/server
source venv/bin/activate
python verify_backup.py /root/backups/berezhok/ИМЯ-АРХИВА.tar.gz
```

Проверка расписания:

```bash
systemctl list-timers berezhok-backup.timer --no-pager
```

### Внешняя копия в Timeweb S3

Для защиты от потери всей виртуальной машины архив можно автоматически отправлять
в приватный S3-бакет. Используется совместимый с S3 адрес
`https://s3.twcstorage.ru`. Рекомендуется создать отдельного пользователя только
для бакета Бережка с правами «Чтение и запись», а не использовать главный ключ
аккаунта.

В `server/.env` добавляются значения из панели Timeweb Cloud:

```env
S3_BUCKET=имя-приватного-бакета
S3_ENDPOINT=https://s3.twcstorage.ru
S3_REGION=ru-1
S3_PREFIX=berezhok
S3_ACCESS_KEY=ключ-дополнительного-пользователя
S3_SECRET_KEY=секрет-дополнительного-пользователя
```

Секреты нельзя добавлять в Git или присылать в публичные сообщения. После
настройки нужно вручную запустить `berezhok-backup.service` и убедиться, что новый
архив появился в бакете. Для бакета следует настроить жизненный цикл с удалением
объектов старше 30 дней.

Официальные инструкции Timeweb Cloud:

- https://timeweb.cloud/docs/s3-storage/manage-storage/create-bucket
- https://timeweb.cloud/docs/s3-storage/manage-storage/additional-users
- https://timeweb.cloud/docs/s3-storage/manage-storage/manage-buckets

### Мониторинг

`/api/health` возвращает `200`, только если доступны база, бот, свежая локальная
копия и достаточный объём диска. После настройки S3 дополнительно проверяется
свежесть внешней копии. Для внешней проверки используется HTTP GET:

```text
https://api.my-berezhok-bot.net.ru/api/health
```

Timeweb Cloud Monitoring умеет проверять этот адрес извне и присылать сообщения
о сбое и восстановлении в Telegram. Внутренняя группа регистрируется командой
`/monitor_here`, которую принимает только аккаунт из `OWNER_USERNAME`.

Официальные инструкции:

- https://timeweb.cloud/docs/monitoring/create
- https://timeweb.cloud/docs/monitoring/manage
- https://timeweb.cloud/docs/account-management/notifications

### Конфигурация сервисов

В `deploy/` хранятся systemd-конфигурации API, бота и ежедневного бэкапа. После
их изменения:

```bash
cp /root/berezhok/deploy/berezhok-api.service /etc/systemd/system/
cp /root/berezhok/deploy/berezhok-bot.service /etc/systemd/system/
cp /root/berezhok/deploy/berezhok-backup.service /etc/systemd/system/
cp /root/berezhok/deploy/berezhok-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now berezhok-api berezhok-bot berezhok-backup.timer
```

Проверка миграции и API фотографий:

```bash
pip install -r server/requirements-dev.txt
python -m unittest server/test_app.py -v
```
