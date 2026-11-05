#!/usr/bin/env python3
"""
Автоматизированный комплекс тестов модуля корпоративной безопасности,
аутентификации (AuthN), RBAC и WORM-аудита (SHA-256 Ledger).
Использует только стандартную библиотеку Python (urllib.request).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"


def req(
    method: str,
    path: str,
    data: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body_bytes = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as err:
        content = err.read().decode("utf-8")
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"detail": content}
        return err.code, parsed


def run_tests():
    print("=================================================================")
    print(" ТЕСТИРОВАНИЕ КОРПОРАТИВНОЙ БЕЗОПАСНОСТИ, RBAC И АУДИТА ")
    print("=================================================================\n")

    # 1. Health check
    code, health = req("GET", "/api/health")
    assert code == 200, f"Health check failed: {code}"
    print(f"[1] Сервер активен: engine={health.get('engine')}, products={health.get('products')}, users={health.get('users')}")

    # 2. Неавторизованный запрос к защищенному эндпоинту -> 401
    code, err = req("GET", "/api/products")
    assert code == 401, f"Ожидался 401, получен {code}"
    print(f"[2] Запрос без токена к /api/products вернул 401: {err.get('detail')}")

    # 3. Аутентификация с неверным паролем -> 401
    code, err = req("POST", "/api/auth/token", {"username": "admin", "password": "wrongpassword"})
    assert code == 401, f"Ожидался 401, получен {code}"
    print(f"[3] Неверный пароль вернул 401: {err.get('detail')}")

    # 4. Аутентификация для всех ролей
    tokens = {}
    passwords = {
        "admin": "admin123",
        "manager": "manager123",
        "storekeeper": "store123",
        "qc": "qc123",
        "worker": "worker123",
    }
    print("\n[4] Проверка аутентификации всех ролей пользователей:")
    for username, pwd in passwords.items():
        code, res = req("POST", "/api/auth/token", {"username": username, "password": pwd})
        assert code == 200, f"Ошибка логина для {username}: {code} {res}"
        assert "access_token" in res
        role = res["user"]["role"]
        tokens[username] = res["access_token"]
        print(f"    [+] {username:12} -> вход успешен | роль: {role:12} | JWT: {res['access_token'][:18]}...")

    # 5. Проверка /api/auth/me
    code, me = req("GET", "/api/auth/me", token=tokens["manager"])
    assert code == 200 and me["username"] == "manager"
    print(f"\n[5] /api/auth/me проверен для manager: {me['full_name']} (цех: {me.get('workshop_name')})")

    # 6. Ролевой контроль доступа (RBAC):
    print("\n[6] Проверка ролевых прав (RBAC matrix):")

    # 6.1 Worker (только чтение/сканирование)
    code, prods = req("GET", "/api/products", token=tokens["worker"])
    assert code == 200, f"Worker failed to list products: {code}"
    print(f"    [+] Worker: чтение товаров разрешено ({len(prods)} товаров)")

    code, err = req("POST", "/api/products", {"name": "Запрещенный товар"}, token=tokens["worker"])
    assert code == 403, f"Worker allowed to create product! {code}"
    print(f"    [+] Worker: создание товара заблокировано (403): {err.get('detail')[:60]}...")

    code, err = req("POST", "/api/tracking/move", {"product_id": 1, "to_location_id": 2, "quantity": 1}, token=tokens["worker"])
    assert code == 403, f"Worker allowed to move stock! {code}"
    print(f"    [+] Worker: перемещение остатка заблокировано (403): {err.get('detail')[:60]}...")

    # 6.2 Storekeeper (приемка и перемещения)
    code, err = req("POST", "/api/products", {"name": "Запрещенный товар завсклада"}, token=tokens["storekeeper"])
    assert code == 403, f"Storekeeper allowed to create product! {code}"
    print(f"    [+] Storekeeper: создание товара заблокировано (403)")

    if prods:
        test_prod_id = prods[0]["id"]
        code, move_res = req(
            "POST",
            "/api/tracking/move",
            {
                "product_id": test_prod_id,
                "to_location_id": 2,
                "quantity": 1,
                "stage": "warehouse",
                "action": "Приемка на склад",
                "note": "Тестовая операция завсклада",
            },
            token=tokens["storekeeper"],
        )
        assert code == 200, f"Storekeeper move failed: {code} {move_res}"
        print(f"    [+] Storekeeper: перемещение товара #{test_prod_id} успешно выполнено (200)")

    # 6.3 Manager (создание и изменение номенклатуры)
    code, new_prod = req(
        "POST",
        "/api/products",
        {
            "name": "Тестовая пряжа хлопок Эко 2026",
            "category": "Пряжа",
            "unit": "бобина",
            "price": 185.0,
            "cost_price": 110.0,
            "initial_quantity": 25,
        },
        token=tokens["manager"],
    )
    assert code == 201, f"Manager failed to create product: {code} {new_prod}"
    created_id = new_prod["id"]
    print(f"    [+] Manager: успешно создал товар #{created_id} «{new_prod['name']}» (201)")

    # Manager не может удалять товар
    code, err = req("DELETE", f"/api/products/{created_id}", token=tokens["manager"])
    assert code == 403, f"Manager allowed to delete! {code}"
    print(f"    [+] Manager: удаление товара заблокировано (403): {err.get('detail')[:60]}...")

    # Manager не может управлять пользователями
    code, err = req("GET", "/api/users", token=tokens["manager"])
    assert code == 403, f"Manager allowed to list users! {code}"
    print(f"    [+] Manager: доступ к управлению пользователями заблокирован (403)")

    # 6.4 QC Inspector (контроль качества)
    code, qc_res = req(
        "PUT",
        f"/api/products/{created_id}/qc",
        {"qc_status": "Брак (на доработку)", "note": "Выявлен дефект окраски"},
        token=tokens["qc"],
    )
    assert code == 200, f"QC failed to update status: {code} {qc_res}"
    print(f"    [+] QC: успешно зафиксировал статус ОТК «{qc_res['qc_status']}» (200)")

    # QC не может изменять цену или название
    code, err = req("PUT", f"/api/products/{created_id}", {"price": 999.0}, token=tokens["qc"])
    assert code == 403, f"QC allowed to edit price! {code}"
    print(f"    [+] QC: попытка изменения цены заблокирована (403): {err.get('detail')[:60]}...")

    # 6.5 Admin (полный CRUD, пользователи, удаление)
    code, users_list = req("GET", "/api/users", token=tokens["admin"])
    assert code == 200, f"Admin failed to list users: {code}"
    print(f"    [+] Admin: доступ к управлению пользователями разрешен ({len(users_list)} пользователей)")

    code, del_res = req("DELETE", f"/api/products/{created_id}", token=tokens["admin"])
    assert code == 200, f"Admin failed to delete product: {code}"
    print(f"    [+] Admin: удаление товара #{created_id} успешно выполнено (200)")

    # 7. Неизменяемый аудит (Audit Trail & SHA-256 Ledger)
    print("\n[7] Проверка неизменяемого аудита (Audit Trail & SHA-256 Chain):")
    code, audit_logs = req("GET", "/api/audit-logs", token=tokens["admin"])
    assert code == 200, f"Failed to get audit logs: {code}"
    print(f"    [+] Всего зафиксировано {len(audit_logs)} операций в журнале аудита")

    for entry in audit_logs[:3]:
        print(f"        #{entry['id']} [{entry['action_type']:18}] user={entry.get('username'):12} hash={entry['current_hash'][:16]}...")

    # 8. Криптографическая верификация цепочки
    code, verify = req("GET", "/api/audit-logs/verify", token=tokens["admin"])
    assert code == 200, f"Verify endpoint failed: {code}"
    print(f"\n[8] Криптографическая проверка цепочки SHA-256:")
    print(f"    Целостность: {verify.get('verified')}")
    print(f"    Проверено записей: {verify.get('total_records')}")
    print(f"    Хеш вершины (head_hash): {verify.get('head_hash')}")
    print(f"    Сообщение: {verify.get('message')}")
    assert verify.get("verified") is True, "Цепочка аудита повреждена!"

    print("\n=================================================================")
    print(" ВСЕ ТЕСТЫ БЕЗОПАСНОСТИ, RBAC И SHA-256 АУДИТА ПРОЙДЕНЫ НА 100%! ")
    print("=================================================================")


if __name__ == "__main__":
    run_tests()
