# China Room Furniture Catalog

Мини-каталог из 2200 мебельных позиций Made-in-China с интерактивным расчётом цены импорта, общей базой заявок и админкой закупщика.

## Запуск

```bash
docker build -t china-room-catalog .
docker run --rm -p 8080:8000 -e ADMIN_PASSWORD=change-me -v china-room-data:/data china-room-catalog
```

Каталог доступен на `/`, админка закупщика — на `/admin.html`. Заявки хранятся в SQLite (`/data/orders.db`); для production каталог `/data` должен быть подключён как persistent volume.
