# Проверка совместимости wheel до создания application venv

## Назначение

`validate_wheelhouse()` сохраняет прежние проверки manifest, уникальности
имён и SHA-256, а затем проверяет полные wheel tags: Python, ABI и platform.
Простое сравнение фрагмента `cpNNN` больше не используется.

Рабочий набор целевых tags получает отдельный изолированный процесс точного
`/usr/bin/python3.12` через `packaging.tags.sys_tags()`. Имя каждого wheel
разбирается `packaging.utils.parse_wheel_filename()`, включая составные tags.
Wheel принимается только при непустом пересечении его tags с tags целевого
интерпретатора.

## Доступность `packaging` до venv

Application venv в этот момент ещё не существует. Установщик не обращается в
сеть и не использует test venv или будущий application venv.

После успешной сверки SHA-256 всего wheelhouse контроллер требует ровно один
`packaging-*-py3-none-any.whl`. Точный `/usr/bin/python3.12` запускается с
`-I -S`, добавляет только этот уже проверенный pure-Python wheel в `sys.path`
и выполняет tag-проверку. Если bootstrap wheel отсутствует, неоднозначен,
повреждён или не импортируется, установка завершается понятным fail-closed
сообщением; разрешающего fallback нет.

## Диагностика отказа

Для несовместимого или некорректно названного wheel выводятся только безопасное
basename и фиксированная причина:

- `invalid wheel filename`;
- `no supported Python/ABI/platform tag`.

Пути, команды, environment, содержимое manifest и секреты не выводятся.

## Локальная проверка

Переносимые unit-тесты передают явный набор целевых tags CPython 3.12 / Linux
x86_64. Они покрывают исходный `cryptography ... cp311-abi3`, `cp312-cp312`,
universal и составные tags, а также отказы для `cp311-cp311`, `cp313-abi3`,
Windows, macOS, ARM64, несовместимой glibc, некорректного имени, manifest,
SHA-256 и недоступной bootstrap-зависимости.

Фактический `sys_tags()` на `/usr/bin/python3.12` Linux и полный install
lifecycle остаются gate будущего disposable run и в этой локальной правке не
заявляются как выполненные.

