# Хранение секретов SSE-QA

## Реализованный контракт

Установщик принимает исходный JSON только как краткоживущий вход защищённого
receiver. До первой мутации сервера он проверяет наличие штатного host key
systemd и преобразует семь чувствительных значений через
`systemd-creds encrypt --with-key=host`. Открытые значения передаются
`systemd-creds` через stdin, а не через argv или журнал.

Постоянно сохраняются только host-bound encrypted credentials в
`/etc/credstore.encrypted/sse-qa/*.cred` с каталогом `0700` и файлами `0600`.
Права и владелец временного файла задаются до записи первого байта. Новый файл
публикуется атомарным hard-link без замены: если destination появился между
проверкой и публикацией, чужой файл сохраняется, а собственный temporary
удаляется. Разрешённая замена root-owned journal/kill-switch остаётся отдельным
режимом после проверки текущего содержимого. Symlink, существующий чужой объект,
неполный write или ошибка fsync завершают операцию fail-closed.

Workflow устанавливает `umask 077` непосредственно в шаге формирования release
package и передаёт JSON builder-у через stdin. Builder независимо от umask
создаёт temporary archive с режимом `0600` до первого байта, fsync-ит и
публикует его без замены. Receiver открывает настоящий `stdin=PIPE`, держит
payload в памяти и пересылает его контроллеру через stdin `systemd-run --pipe`;
отдельный plaintext `secrets.json` на сервере и значение в argv не создаются.
Шаг `always()` удаляет transport archive после передачи или ошибки.

Transport archive краткоживущий, но **не зашифрованный**: JSON внутри tar.gz
остаётся открытым. Его защита — режим `0600`, закрытый GitHub runner, отсутствие
значения в argv/log и обязательное удаление. Это не следует смешивать с
зашифрованным постоянным хранением на сервере.

- Django WSGI/ASGI/reconciliation получают `django_secret_key`, пароль роли
  PostgreSQL и пароль Redis через `LoadCredentialEncrypted=`. Settings читают
  только fixed allowlist из `$CREDENTIALS_DIRECTORY` с `O_NOFOLLOW`.
- PIN двух синтетических пользователей расшифровывается только в краткоживущем
  bootstrap-процессе и не записывается в `app.env`.
- PostgreSQL получает пароли через stdin `psql`; рабочий экземпляр хранит
  штатные SCRAM-verifier. SHA-256 больше не используется как защита пароля.
- Redis получает пароль через systemd credential. Launcher создаёт ACL в
  анонимном `memfd`, передаёт Redis только `/proc/self/fd/N` и не создаёт ACL
  на диске.
- bcrypt-verifier Basic Auth также хранится зашифрованно. При `enable` он
  атомарно появляется только в tmpfs `/run/sse-qa-nginx/htpasswd` (`0640`,
  `root:www-data`) и удаляется при `disable`. Он ведётся отдельно в
  `runtime_files`, поэтому его отсутствие после disable/перезагрузки допустимо;
  присутствующий изменённый или чужой verifier по-прежнему блокирует операцию.

Ownership journal содержит SHA-256 опубликованных файлов, включая ciphertext.
Это контроль целостности/владения, а не хеширование секрета. Изменённый,
чужой или повреждённый ciphertext блокирует verify/remove; потерянный host key
блокирует decrypt. Автоматического восстановления из ciphertext нет: требуется
повторная разрешённая установка с новым набором секретов.

## Предварительное условие и границы проверки

На целевом Linux должен существовать `/var/lib/systemd/credential.secret` с
владельцем root и без group/other bits. Обычный installer не создаёт и не
удаляет host key. Одноразовый disposable-сценарий создаёт его штатной командой
`systemd-creds setup` и удаляет после zero-residue проверки.

Локальные Windows-тесты не доказывают реальную работу systemd encrypted
credentials, memfd, владельцев POSIX и полного install/enable/disable/remove.
После независимого review нужен новый разрешённый disposable Linux cycle;
прежний Linux PASS относится к предыдущей версии установщика.
