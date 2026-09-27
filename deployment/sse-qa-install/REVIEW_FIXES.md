# Закрытие замечаний независимого install-review

## Disposable workflow R2 — 28.09.2026

По независимой проверке нового disposable workflow исправлены шесть адресных
дефектов без изменения принятой реализации установщика R4:

1. `wheelhouse.sha256` формируется из basename (`sha256sum *.whl`) и проверяется
   настоящим `validate_wheelhouse()`;
2. полный package suite запускается в новом Python 3.12 venv, установленном
   только из зафиксированного wheelhouse;
3. nginx явно запускается и подтверждается отдельным evidence до lifecycle;
4. journal normal/fault/cancel фильтруется по сохранённому systemd
   `InvocationID`, а не по общей истории unit;
5. evidence manifest исключает себя и временный файл, затем дважды проверяется
   через `sha256sum -c --status`;
6. все сгенерированные значения маскируются до использования, raw cycle log до
   sanitization не выводится, scanner работает в quiet mode, а emit возможен
   только после успешного seal.

Реальный GitHub Actions/Linux прогон после этих изменений не выполнялся и
остаётся `NOT_RUN`.

Проверяемые рецензии:

- `evidence/input/SSE_QA_install_review_for_Codex.md`;
- `evidence/input/SSE_QA_R2_review_for_Codex.md`;
- `evidence/input/SSE_QA_R3_review_for_Codex.md`;
- `evidence/input/SSE_QA_R4_review_and_next_step.md`.

| Группа | Исправление | Адресная проверка |
|---|---|---|
| QA-1 | preinstall unit render с безопасными runtime-заглушками; postinstall original verify; точный Python 3.12; live mount/cgroup/SQL/Redis/fixture/kill-switch phase checks | `test_preinstall_systemd_verify_substitutes_only_runtime_dependencies`, `test_local_installed_verification_checks_hashes_and_phase` |
| QA-2 | Redis ACL `0640 root:sseqa` + read test; canonical `Самосвал`/`Экскаватор`; два логина, экраны, trip, catch-up и SSE smoke | `test_redis_acl_permissions_and_canonical_equipment_types`; `smoke_sse_qa` |
| QA-3 | ownership journal до первой мутации; per-command timeout; SIGTERM cleanup; receiver process-group termination; fixed partial disable/remove; two fault points | `test_fault_injection_is_fail_closed`; disposable cycle faults/cancel |
| QA-4 | один полный conflict list; ожидаемый hash фиксируется до записи; altered/foreign objects fail closed; mount/PG paths verified before remove | `test_preflight_rejects_every_managed_path_before_writing`, `test_owned_file_guard_refuses_changed_object` |
| QA-5 | PyPI/wheelhouse только для install; enable/smoke/disable/remove используют exact one-file control ZIP | `test_control_package_needs_no_runtime_or_package_index` |
| QA-6 | installer + PostgreSQL + Redis + enable + installed verify/smoke находятся в одном parent `sse-qa.slice` с exact 1 CPU/2 GiB/swap 0/256 tasks; bootstrap/persistent slice lifecycle fail-closed; receiver остаётся снаружи; постоянные QA logs внутри 6 GiB image | `test_installer_postgres_and_redis_share_exact_parent_budget`, `test_exact_parent_budget_rejects_wrong_cpu_quota`, `test_bootstrap_slice_lifecycle_preserves_only_successful_install`, `test_install_enable_verify_and_smoke_use_shared_parent_slice`, `test_enable_verifies_before_and_after_start_and_rolls_back_on_error` |

Локально выполнены только Windows unit/contract/archive checks. Подготовленный
destructive integration cycle допускается исключительно на новой disposable
Ubuntu 24.04 VM с marker `/run/sse-qa-disposable-test`, без production paths и
без сетевого доступа к production. Его фактический PASS ещё отсутствует.

## Остатки ревью R2

| Группа | Исправление | Адресная проверка |
|---|---|---|
| R2-1 | логические POSIX-пути больше не преобразуют буквальный `\\` в mount-unit; conflict/copy/hash/remove используют одно имя | `test_rooted_preserves_literal_posix_backslash`; полный exact-name цикл `test_mount_unit_conflict_copy_verify_remove_uses_exact_linux_name` выполняется только на POSIX |
| R2-2 | валидные `+79…` учётки, модели/кубатура/плотность; production cursor; serialized `type`; строгая сверка нового `trip_id` и version | `test_seed_business_smoke.py`; runtime Django `users.test_sse_qa_seed` — 3/3 PASS |
| R2-3 | cleanup двухфазный и fail-closed: mount/PG/files проверяются до остановки и повторно до `pg_dropcluster`/удалений | `test_cleanup_foreign_mount_fails_before_destructive_calls`, `test_cleanup_changed_owned_file_fails_before_destructive_calls`, `test_cleanup_rechecks_guard_after_stopping_services` |
| R2-4 | disable не сообщает успех без подтверждённой остановки; timeout receiver всегда завершает process group и проверяет unit; partial dependency start и enable rollback попадают под cleanup | `test_shutdown_contract.py` — 4/4 PASS |
| R2-5 | installer, PG/Redis, enable, installed verify и smoke входят в общий parent `sse-qa.slice`; после reboot slice активируется on-demand до чтения cgroup; проверяются точные лимиты и реальная cgroup membership | `test_installer_postgres_and_redis_share_exact_parent_budget`, `test_exact_parent_budget_rejects_wrong_cpu_quota`, `test_receiver_reads_exact_slice_control_group_and_disposable_checks_child`, `test_enable_process_is_child_of_shared_slice`, `test_installed_smoke_process_is_child_of_shared_slice`, `test_enable_verifies_before_and_after_start_and_rolls_back_on_error` |

## Остатки ревью R3

| Группа | Исправление | Адресная проверка |
|---|---|---|
| R3-1 | parent cgroup читается из live `ControlGroup` и обязан точно равняться `/sse.slice/sse-qa.slice`; child/process — только `parent/unit`; receiver использует сегментную at-or-below проверку и не принимает похожий prefix | `test_operation_scope_rejects_process_outside_real_systemd_hierarchy`, `test_unit_cgroup_rejects_similar_but_non_child_path`, `test_receiver_rejects_actual_qa_slice_and_similar_prefix_is_not_a_child`, `test_receiver_reads_exact_slice_control_group_and_disposable_checks_child` |
| R3-2 | весь `enable_sse_qa` выполняется как фиксированный `sse-qa-enable.service` внутри parent slice; обе внутренние проверки и Django subprocess наследуют тот же бюджет; ошибка вызывает подтверждаемый rollback | `test_enable_process_is_child_of_shared_slice`, `test_enable_verifies_before_and_after_start_and_rolls_back_on_error` |
