# Бережок

Telegram Mini App со списком покупок «на подумать» и ботом-напоминателем.

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

Проверка миграции и API фотографий:

```bash
pip install -r server/requirements-dev.txt
python -m unittest server/test_app.py -v
```
