# Qwen parallel OCR — Phase A, 5 ноября 2026

База: `e711d6a73831e39a4cb4ccd314a9122125794379`.
Ветка: `codex/qwen-parallel-phase-a`. Изменения не закоммичены.
Это handoff для независимого ревью GPT-6; разрешение включать платный параллельный OCR не запрашивалось и не использовалось.

## 1. Воспроизведение на production SHA

Исходные 78 тестов orchestration/Qwen adapter проходят. Три новые проверки на чистом коде `e711d6a` воспроизводят нарушение лимитов:

| Управляемая задержка | Ограничение за окно 0,2 с | Фактические старты |
| --- | --- | --- |
| Запись dispatch checkpoint после `_RateGate.acquire` | 2 запроса / 40 токенов | 4 запроса / 80 зарезервированных токенов |
| Подготовка client после shared quota admission, два документа | RPM = 2 | 4 запроса |
| Та же подготовка, два документа | TPM = 768, резерв 384 на запрос | 4 запроса / 1536 зарезервированных токенов |

Причина — оба лимитера учитывали время admission. Разрешение могло истечь, пока запрос ещё ждал checkpoint, другую квоту или поток. Новые разрешения после этого присоединялись к запоздавшим старым отправкам. Само наличие двух лимитеров не закрывает этот промежуток.

Дополнительно оркестратор записывал `request_started=True` раньше ожидания quota адаптера и локальной проверки изображения. После отмены эта запись могла ошибочно выглядеть как отправка. `asyncio.to_thread()` допускал конкурентные PyMuPDF-вызовы; системный executor на этой машине также не обеспечивал десять одновременно заблокированных HTTP-вызовов.

Лог воспроизведения: `tmp/phase-a/baseline-reproductions.log`. Только synthetic adapters и fake transport, без Qwen HTTP.

## 2. Исправление dispatch

`dispatch.py` предоставляет общий алгоритм rolling quota с permit, защищённый `threading.Lock`. `_RateGate` и credential-scoped `QwenVisionQuotaRegistry` используют этот алгоритм. Admission удерживает RPM/TPM до завершения попытки; cooldown начинается после её завершения. Известная отмена до transport освобождает permit. Известные и неопределённые отправленные попытки сохраняют резерв. Это сознательно более консервативная пропускная способность для медленных запросов.

Доказательство для окна W: любой запрос, вошедший в transport за последние W секунд, либо ещё выполняется и удерживает permit, либо завершился менее W секунд назад и удерживает cooldown. Admission атомарно проверяет число таких permits и сумму их token reservations. Поэтому задержка между admission и POST не создаёт дополнительных разрешений. Для TPM требуется корректный верхний предел токенов; локальный код не может гарантировать потребление провайдера при неверном upper bound.

Новая последовательность:

1. Ограничить число страниц в работе; подготовить изображение; зарезервировать стоимость; сохранить `running` без отправки.
2. Получить document permit и shared credential permit; получить место в HTTP-пуле.
3. Client валидирует PNG и тело запроса. Callback на исходном event loop повторно проверяет отмену, PDF, остановку и атомарный request cap; сохраняет `dispatch_intent_at` через существующую fsync/replace запись. После записи повторно проверяет отмену и PDF.
4. Непосредственно перед единственным вызовом transport отмечается вход в transport. Только завершённая обработка transport позволяет записать `request_started` и `dispatched_at` как наблюдаемые факты. Это не подтверждение, что сервер принял все байты.
5. Сохранить результат/ошибку страницы независимо; учесть usage или сохранить неизвестную стоимость. Никакого автоматического повторного POST.

HTTP-пул имеет ровно 10 потоков и отдельный общий предел 10 submitted calls. Он не зависит от размера default executor и сохраняет contextvars. Отмена дожидается завершения синхронного transport, прежде чем освободить его ресурсы.

Успешные checkpoint-страницы не переписываются при обычном resume. History quota восстанавливается в document gate и shared registry; идентификатор резерва включает checkpoint identity, страницу и индекс попытки, не часы. Повторное открытие не создаёт одинаковые history reservations в registry. Защищён также повторный запуск одного объекта CheckpointStore: конкурентный запуск не может закрыть lock владельца.

## 3. Renderer и пределы ресурсов

Один лениво создаваемый `spawn`-процесс последовательно выполняет PyMuPDF rendering, чтение количества страниц и преобразование geometry в PDF points. Это изолирует AI-first PDF-операции от app threads. Семантика page regions не меняется. В процесс передаются путь, номер и ограничения; возвращается только PageImage/metadata, без Document/Page/Pixmap handles.

Перед submission нужен общий render slot; одновременно существует не более одного незавершённого задания renderer. Slot освобождает completion callback executor, поэтому синхронное чтение metadata не блокирует освобождение async render. Отмена drain-ит native operation. Смерть worker даёт безопасную ошибку без HTTP; следующий разрешённый запуск может создать новый worker, а failed page сама не повторяется. Parent watcher завершает idle renderer после смерти app-процесса.

Для документа с C workers одновременно удерживается не более C страниц; C ограничено 10. Для всех документов и event loops одного процесса общий предел — 10 страниц от начала render до settlement/checkpoint. Очередь содержит только номера страниц, максимум 2C на документ. Нет отдельной неограниченной очереди подготовленных изображений.

Логический объём сохранённых PNG ограничен суммой лимитов не более десяти страниц; наибольший допустимый `max_image_bytes` в модели — 100 MB, то есть абсолютный верхний предел этих PNG — 1 GB. Это верхняя граница, не целевой расход. Есть дополнительно одна временная копия при IPC, один native pixmap/PNG encoder и ограниченные HTTP/Base64/response buffers. Это не жёсткий OS RSS cap для произвольного сложного PDF; native memory и зависание renderer требуют отдельного эксплуатационного gate. В пилоте нужно использовать реальные разрешённые размеры и проверить packaging/startup/shutdown worker на целевой машине.

## 4. State machine, данные и бюджет

- `running` без intent/старого `request_started` после остановки возвращается в `pending`: платная попытка ещё не разрешалась.
- Durable intent без известного результата становится `unknown_outcome`, даже если `request_started=False`. Request cap, budget и quota учитывают эту неоднозначность. Resume не повторяет её автоматически.
- Успехи других страниц сохраняются атомарно и независимо; source/page identity проверяется до публикации, полный source digest — в конце.
- Budget ledger атомарно проверяет `spent + reserved + new <= document_cap`. Неизвестная стоимость удерживает максимальный резерв. Известный отказ до transport возвращает резерв. Legacy unknown без cost reservation блокирует новый расход полным document cap.
- Budget — на один checkpoint/run. Общий account budget и координация разных процессов в этой фазе не реализованы; нельзя трактовать document cap как общий account cap. Гарантия предварительного потолка предполагает корректные token upper bounds и заданные тарифы.
- Повторная попытка unknown/failed/incomplete требует существующего явного `retry_pages`; предыдущие attempts/extraction сохраняются для аудита.

Три subprocess-теста убивают отдельный app-процесс после первой успешной страницы: до intent второй, после её intent и внутри fake HTTP transport. Первая страница остаётся побайтно той же. До intent вторая безопасно обрабатывается после resume; в двух остальных случаях остаётся unknown с резервом 0,0896 RUB и нулём новых POST.

Ответ, полученный процессом, но ещё не записанный атомарно, нельзя обещать восстановить после hard kill: такая попытка остаётся unknown. Результаты, уже сохранённые checkpoint, восстанавливаются. Нельзя исправлять этот разрыв автоматическим платным повтором.

Старые checkpoints читаются, extraction/processing/adapter identity и manifest format 2 сохранены. Новое опциональное поле intent не поддерживается старым strict reader: откат старого runtime на новые checkpoints завершится отказом чтения, а не автоматическим повтором. Для выпуска нужна явная политика rollback/совместимости этого поля.

## 5. Проверки и сравнение

Среда: Windows, Python 3.12.10, Node 24.19.0. Все новые запросы идут в контролируемый fake transport. Полные Python-прогоны дополнительно запрещают внешние socket connections; отдельные AVERON_DATA_DIR и временные каталоги изолируют fixtures от пользовательских данных.

| Проверка | Результат |
| --- | --- |
| Исходный профиль orchestration + Qwen adapter | 78 passed |
| Три новых limiter regressions на production-коде | 3 ожидаемых падения: 4 старта при разрешённых 2 |
| Полный non-integration Python baseline | 3602 passed, 5 failed, 2 skipped, 1 deselected; 559,26 с |
| Полный non-integration Python с Phase A | 3622 passed, те же 5 failed, 2 skipped, 1 deselected; 570,09 с |
| Повторный limiter/render/adapter профиль после изоляции metadata | 98 passed; 41,52 с |
| Финальные workflow, reliability, controlled pilot, XLSX, atomic bulk review | 122 passed; 96,23 с |
| Повторный adapter + safety после стабильных reservation IDs | 56 passed; 23,94 с |
| Последний safety-профиль, включая legacy unknown budget | 21 passed |
| JavaScript baseline и Phase A | по 23 passed, включая 2 headless Edge harnesses |

Последние узкие прогоны покрывают финальные изменения metadata/ID/legacy budget после полного прогона. Полный набор после этих последних правок повторно не запускался.

Список пяти падений совпал по testcase ID в обоих полных прогонах:

- `test_one_c_history_sourcing_ui.py::test_project_result_status_and_source_cells_wrap_with_internal_table_scroll` — старый глобальный запрет `overflow-x:hidden` против существующего CSS.
- `test_product_ui.py::test_review_layout_contains_overflow_inside_workspace_and_pdf_pane` — тот же конфликт CSS-проверки.
- `test_processing_coordinator.py::{test_local_profile_resolves_tesseract_adapter,test_settings_processing_mode_influences_resolver,test_local_mode_rejects_cloud_llm_combination}` — Tesseract недоступен в окружении.

Не исправлялись: UI и локальный OCR вне согласованного scope. Две skipped Python-проверки требуют частных real XLSX/cache fixtures; integration test требует real PDF и исключён. Отдельный `ai_first_vertical_workspace_browser.cjs` не запускался: нужен работающий авторизованный ASGI fixture со screenshot directory; это не один из двух пройденных автономных browser harnesses.

Новые проверки также охватывают несколько документов одной credential, разные event loops/потоки, строгие RPM и TPM по отдельности, pinned permits дольше окна, отмену до/после отправки, timeout после отправки, request cap при 10 workers, budget exhaustion/resume, renderer error/death, out-of-order completion, duplicate checkpoint run и отсутствие повторных POST после hard kill.

Deterministic snapshots на чистом baseline и Phase A совпали для draft, Human Review, raw observations и всех XML-частей pilot/diagnostic XLSX. Зафиксированы только event UUID/time; у XLSX исключены ZIP timestamps и docProps created/modified. Остальное содержимое XML сравнивается полностью. Snapshot JSON и synthetic XLSX находятся в `tmp/phase-a/snapshot-{baseline,final}`.

## 6. Фактическая конкурентность

Fake provider задерживает каждый ответ на 1 секунду. Барьер находится внутри `post`: все N запросов должны войти в transport до возврата любого из них. Последовательная реализация не может пройти такой барьер.

| Страниц / concurrency | Одновременно в transport | Wall time, с | Разброс стартов POST, с | Peak PNG bytes |
| --- | --- | --- | --- | --- |
| 1 | 1 | 1,141 | 0,000 | 1184 |
| 2 | 2 | 1,110 | 0,000 | 2368 |
| 4 | 4 | 1,172 | 0,063 | 4736 |
| 8 | 8 | 1,297 | 0,157 | 9472 |
| 10 | 10 | 1,375 | 0,203 | 11840 |

Десять synthetic страниц заняли 1,21 времени одной. Это доказательство overlap и работы scheduler при достаточных квотах, не прогноз производительности реального Qwen или тяжёлого PDF. Cold spawn исключён подготовкой identity до замера; job всё равно проверяет source при старте. Два документа по десять страниц совместно достигают 10, а не 20 одновременных transport calls. Renderer submissions peak = 1.

## 7. Что остаётся до безопасного live concurrency=10

Release gate из `ai_first_multiworker_design.md` не закрыт и не изменён. Registry, resource caps и budget ledger process-local. Несколько документов безопасно используют shared registry в одном процессе; два независимых app-процесса или две разные registry не координируются. Восстановление учитывает загруженные checkpoints, но не обеспечивает account-wide историю ещё не открытых документов после полного рестарта. OS lock защищает один checkpoint, не provider account.

Минимальное продолжение — одна private SQLite coordination DB и единственный диспетчер, а не многоузловая система:

1. Durable account-scoped RPM/TPM/cost reservations, включая историю всех runs, до любого нового POST после рестарта. Атомарное резервирование и settlement; отдельное подтверждение настоящих provider quotas, тарифов и token ceilings.
2. Unique page-attempt identity, durable intent, ownership/fencing и запрет stale publication. Доказать двумя app-процессами, что второй не может обойти owner/quota/budget и повторить неизвестную попытку.
3. Сохранить bounded renderer/HTTP resources, добавить проверку справедливого планирования документов/пользователей, native-memory/process lifecycle и rollback checkpoints на целевой сборке.
4. Независимое ревью, затем отдельное разрешение на M4 small paid pilot с потолком стоимости, без автоматических повторов; после него scale validation. Настройка concurrency сама по себе не закрывает gate.

`local_workflow.py` побайтно совпадает с baseline и по-прежнему создаёт live config с `concurrency=1`. Тарифы, prompts, schema, UI, Human Review, supplier sourcing, page regions semantics и export implementation не изменены. Общий DTO теперь допускает до 10 для offline tests/library orchestration; live factory не повышена.

SSH, deploy, реальные Qwen HTTP, push, merge и commit не выполнялись. Работа останавливается на фазе A для независимого ревью.

## Артефакты и повторение

Полный patch: `tmp/phase-a/qwen-parallel-phase-a.patch`. Проверяется на чистом архиве production SHA через `git apply --check`.
Логи/XML, JS results, benchmark JSON и snapshot tools сохранены рядом. Для повторения новых тестов из checkout:

```powershell
python -m pytest tests/test_ai_first_parallel_safety.py tests/test_ai_first_orchestration.py tests/test_ai_first_qwen_vision_adapter.py -q --basetemp=<short-private-temp-path>
```

Для полного сравнения использовать чистый архив `git archive e711d6a`, ту же venv и разные private temp/AVERON_DATA_DIR; команда suite — `python tmp/phase-a/offline_validate.py <root> tests -q -m 'not integration' --junitxml=<output>`. Offline wrapper и остальные вспомогательные tools лежат в review bundle, не в production package. JS runner требует bundled Playwright в NODE_PATH для двух автономных browser harnesses.

## 8. GPT-6 follow-up: PDF waits and legacy renderers

После независимого ревью устранены два замечания Phase A. Async `inspect_pdf_async()` и `_region_pdf_points()` передают hashing, открытие PDF и чтение page geometry в тот же изолированный однопроцессный PyMuPDF worker. Ожидание worker выполняется через asyncio future; общий renderer slot удерживается до фактического завершения native операции. Отмена дожидается операции и только затем возвращает управление, не оставляя работу в очереди или занятый слот. Синхронный `inspect_pdf()` сохранён для CLI/background callers и явно отказывает, если его вызвать из работающего event loop; async route использует async API.

Синхронные renderer adapters снова исполняются через `asyncio.to_thread()`. Общий process-local slot с пределом 1 сериализует их между документами/event loops, включая период после отмены до завершения потока; dedicated process-worker renderer остаётся отдельным существующим путём.

Проверки follow-up: профильные Python tests — 141 passed; полный offline Python профиль — 3628 passed, 5 failed, 2 skipped, 1 deselected (562.11 s); JS harnesses — 23 passed, 1 skipped. Все пять Python отказов совпадают с `e711d6a`: два CSS overflow assertion и три теста локального Tesseract, который недоступен в среде. Разницы в failure testcase ID нет. JUnit XML: `tmp/phase-a/python-review-final.xml`; профильный лог: `tmp/phase-a/python-review-final.log`.

Новые проверки покрывают отзывчивость event loop во время PDF worker ожидания, конкурирующие документы, отмену с drain, сериализацию legacy sync renderers и возврат worker/thread capacity после завершения. Live factory и `local_workflow.py` остаются с concurrency=1. Follow-up не меняет production deployment, budget limits, checkpoint semantics или dispatch limiter.
