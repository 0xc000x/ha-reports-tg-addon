# ICE Monitor — add-on для Home Assistant

Присылает в Telegram новые репорты об активности ICE из нескольких источников — stopice.net и antifreeze.app (включая iceout.org) —
в радиусе заданных точек.

## Установка

1. Настройки → Дополнения → Магазин дополнений → «⋮» → «Репозитории».
2. Добавьте адрес этого репозитория на GitHub.
3. Установите «ICE Monitor», заполните ключ API stopice, токен бота и ID чата, запустите.

Обновления появляются в Home Assistant автоматически после увеличения `version` в `stopice_telegram/config.yaml`.
