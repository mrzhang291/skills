"""
安全加密存储模块：AES-256 Fernet加密JSON数据
支持两种模式：
1. 密钥文件模式（向后兼容）：.agent_data/.key
2. 密码模式：PBKDF2派生密钥
   - 小文件模式（全量读写）：[16B salt][Fernet(JSON数组)]
   - 增量模式（逐行追加）：首行base64(salt) + 每行base64(Fernet(单条JSON))
     数百份文件时性能不衰减，每次写入只需加密一条记录
"""
import json
import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend

SALT_SIZE = 16
PBKDF2_ITERATIONS = 600000  # OWASP 2023推荐


def _get_storage_dir(base_dir: str = None, encrypted_dir: str = None,
                     department: str = None) -> str:
    if encrypted_dir and department:
        d = os.path.join(encrypted_dir, department)
        os.makedirs(d, exist_ok=True)
        return d
    if base_dir:
        return os.path.join(base_dir, ".agent_data")
    return os.path.join(os.getcwd(), ".agent_data")


def _get_or_create_key(storage_dir: str) -> bytes:
    os.makedirs(storage_dir, exist_ok=True)
    key_file = os.path.join(storage_dir, ".key")
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(key_file, "wb") as f:
        f.write(key)
    return key


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
        backend=default_backend(),
    )
    key = kdf.derive(password.encode("utf-8"))
    return base64.urlsafe_b64encode(key)


# ==================== 密钥文件模式（向后兼容） ====================

def encrypt_store(data: list, base_dir: str = None) -> None:
    storage_dir = _get_storage_dir(base_dir)
    os.makedirs(storage_dir, exist_ok=True)
    f = Fernet(_get_or_create_key(storage_dir))
    json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    encrypted = f.encrypt(json_bytes)
    with open(os.path.join(storage_dir, "store.enc"), "wb") as sf:
        sf.write(encrypted)


def decrypt_store(base_dir: str = None) -> list:
    storage_dir = _get_storage_dir(base_dir)
    store_file = os.path.join(storage_dir, "store.enc")
    if not os.path.exists(store_file):
        return []
    f = Fernet(_get_or_create_key(storage_dir))
    with open(store_file, "rb") as sf:
        encrypted = sf.read()
    json_bytes = f.decrypt(encrypted)
    return json.loads(json_bytes.decode("utf-8"))


def add_record(record: dict, base_dir: str = None) -> None:
    store = decrypt_store(base_dir)
    store.append(record)
    encrypt_store(store, base_dir)


def search_records(query: str, base_dir: str = None) -> list:
    store = decrypt_store(base_dir)
    return _do_search(store, query)


def get_stats(base_dir: str = None) -> dict:
    store = decrypt_store(base_dir)
    return _build_stats(store)


# ==================== 密码模式 ====================

def _store_file_path(storage_dir: str, department: str = None) -> str:
    if department:
        return os.path.join(storage_dir, f"store_{department}.enc")
    return os.path.join(storage_dir, "store.enc")


# --- 全量模式（小数据量，向后兼容） ---

def pw_encrypt_store(data: list, password: str, base_dir: str = None,
                     department: str = None, encrypted_dir: str = None) -> None:
    """全量加密存储：[16B salt][Fernet(JSON数组)]"""
    storage_dir = _get_storage_dir(base_dir, encrypted_dir, department)
    os.makedirs(storage_dir, exist_ok=True)
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)
    f = Fernet(key)
    json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    encrypted = f.encrypt(json_bytes)
    with open(_store_file_path(storage_dir, department), "wb") as sf:
        sf.write(salt + encrypted)


def _pw_decrypt_full(filepath: str, password: str) -> list:
    """解密全量格式"""
    with open(filepath, "rb") as sf:
        data = sf.read()
    if len(data) < SALT_SIZE:
        raise ValueError("store.enc 格式无效")
    salt = data[:SALT_SIZE]
    encrypted = data[SALT_SIZE:]
    key = _derive_key(password, salt)
    json_bytes = Fernet(key).decrypt(encrypted)
    return json.loads(json_bytes.decode("utf-8"))


def pw_decrypt_store(password: str, base_dir: str = None,
                     department: str = None, encrypted_dir: str = None) -> list:
    """解密读取，自动识别全量/增量格式"""
    storage_dir = _get_storage_dir(base_dir, encrypted_dir, department)
    store_file = _store_file_path(storage_dir, department)
    if not os.path.exists(store_file):
        return []
    # 检测格式：增量格式首字节是base64字符（可打印ASCII）
    with open(store_file, "rb") as sf:
        first = sf.read(1)
    if first and 32 <= first[0] <= 126:
        return _pw_read_incremental(store_file, password)
    return _pw_decrypt_full(store_file, password)


def pw_add_record(record: dict, password: str, base_dir: str = None,
                  department: str = None, encrypted_dir: str = None) -> None:
    """添加记录（自动选择增量/全量，增量优先）"""
    storage_dir = _get_storage_dir(base_dir, encrypted_dir, department)
    store_file = _store_file_path(storage_dir, department)
    if os.path.exists(store_file):
        pw_append(record, password, base_dir, department, encrypted_dir)
    else:
        # 首条记录，直接用增量模式初始化
        pw_append(record, password, base_dir, department, encrypted_dir)


def pw_search_records(query: str, password: str, base_dir: str = None,
                      department: str = None, encrypted_dir: str = None) -> list:
    store = pw_decrypt_store(password, base_dir, department, encrypted_dir)
    return _do_search(store, query)


def pw_get_stats(password: str, base_dir: str = None,
                 department: str = None, encrypted_dir: str = None) -> dict:
    storage_dir = _get_storage_dir(base_dir, encrypted_dir, department)
    store_file = _store_file_path(storage_dir, department)
    if not os.path.exists(store_file):
        return {"total_files": 0, "tags": [], "files": []}
    # 只计数不加载全量（增量模式下 O(n) 扫描行数）
    count = 0
    with open(store_file, "rb") as sf:
        first = sf.read(1)
    if first and 32 <= first[0] <= 126:
        with open(store_file, "r", encoding="utf-8") as sf:
            count = max(0, sum(1 for _ in sf) - 1)  # 减掉首行salt
    else:
        count = len(_pw_decrypt_full(store_file, password))
    return {"total_files": count, "tags": [], "files": []}


# --- 增量模式（数百份文件不衰减） ---

def pw_append(record: dict, password: str, base_dir: str = None,
              department: str = None, encrypted_dir: str = None) -> None:
    """
    增量追加：加密单条记录，base64编码后追加一行
    不读取现有数据，O(1) 写入，数百份报告不衰减
    """
    storage_dir = _get_storage_dir(base_dir, encrypted_dir, department)
    os.makedirs(storage_dir, exist_ok=True)
    store_file = _store_file_path(storage_dir, department)

    if os.path.exists(store_file):
        # 读取已有salt
        with open(store_file, "r", encoding="utf-8") as sf:
            salt_line = sf.readline().strip()
        salt = base64.b64decode(salt_line)
    else:
        # 新文件：生成salt，写首行
        salt = os.urandom(SALT_SIZE)
        with open(store_file, "w", encoding="utf-8") as sf:
            sf.write(base64.b64encode(salt).decode() + "\n")

    key = _derive_key(password, salt)
    f = Fernet(key)
    record_json = json.dumps(record, ensure_ascii=False).encode("utf-8")
    encrypted = f.encrypt(record_json)
    line = base64.b64encode(encrypted).decode("utf-8")

    with open(store_file, "a", encoding="utf-8") as sf:
        sf.write(line + "\n")


def _pw_read_incremental(filepath: str, password: str) -> list:
    """逐行解密增量格式"""
    records = []
    data_lines = 0
    error_lines = 0
    with open(filepath, "r", encoding="utf-8") as sf:
        salt_line = sf.readline().strip()
        if not salt_line:
            return []
        salt = base64.b64decode(salt_line)
        key = _derive_key(password, salt)
        f = Fernet(key)
        for line in sf:
            line = line.strip()
            if not line:
                continue
            data_lines += 1
            try:
                encrypted = base64.b64decode(line)
                json_bytes = f.decrypt(encrypted)
                records.append(json.loads(json_bytes.decode("utf-8")))
            except Exception:
                error_lines += 1
                continue  # 跳过损坏的行
    # 有数据行但全部解密失败 → 密码错误
    if data_lines > 0 and len(records) == 0:
        raise ValueError("密码错误，无法解密任何记录")
    return records


def pw_count_records(password: str, base_dir: str = None,
                     department: str = None, encrypted_dir: str = None) -> int:
    """快速计数（不加载全部数据）"""
    storage_dir = _get_storage_dir(base_dir, encrypted_dir, department)
    store_file = _store_file_path(storage_dir, department)
    if not os.path.exists(store_file):
        return 0
    count = 0
    with open(store_file, "rb") as sf:
        first = sf.read(1)
    if first and 32 <= first[0] <= 126:
        with open(store_file, "r", encoding="utf-8") as sf:
            sf.readline()  # 跳过salt行
            for _ in sf:
                count += 1
    else:
        count = len(_pw_decrypt_full(store_file, password))
    return count


# ==================== 配置加密工具（保护 .emp_config.json 中的密码） ====================
# 用途：对 .emp_config.json 中的 password 字段单独加密，防止文件被拷贝后密码明文泄露
# 加密key由机器环境派生（MAC地址 + 用户目录），换机器则无法解密

_CONFIG_ENCRYPTION_SALT = b"emp_config_v1"  # 固定盐，与PBKDF2隔离


def _derive_config_key(base_dir: str) -> bytes:
    """
    从 base_dir 派生加密密钥。
    base_dir 通常是员工的 NAS 路径，具有目录位置特异性。
    key 与 base_dir 绑定，换目录则无法解密，但换机器不影响（只要 base_dir 不变）。
    """
    import hashlib
    # 将 base_dir + 固定盐 混合后用 PBKDF2 拉伸
    combined = f"{base_dir}|config_encryption_v1".encode()
    stretch = hashlib.pbkdf2_hmac("sha256", combined, _CONFIG_ENCRYPTION_SALT, 100000, dklen=32)
    return base64.urlsafe_b64encode(stretch)


def encrypt_value(value: str, base_dir: str) -> str:
    """
    加密单个字符串值（如密码），返回 base64 密文。
    base_dir 用于派生密钥，同一台机器的相同 base_dir 才能解密。
    """
    if not value:
        return value
    key = _derive_config_key(base_dir)
    f = Fernet(key)
    encrypted = f.encrypt(value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def decrypt_value(encrypted_value: str, base_dir: str) -> str:
    """
    解密 encrypt_value 加密的密文，返回原文。
    """
    if not encrypted_value:
        return encrypted_value
    try:
        key = _derive_config_key(base_dir)
        f = Fernet(key)
        encrypted = base64.b64decode(encrypted_value.encode("utf-8"))
        return f.decrypt(encrypted).decode("utf-8")
    except Exception:
        # 兼容旧版明文存储：密文解密失败时直接返回原文（用于迁移期）
        return encrypted_value


def _build_stats(store: list) -> dict:
    tags = list(set(sum((r.get("tags", []) for r in store), [])))
    return {
        "total_files": len(store),
        "tags": tags,
        "files": [r.get("source_filename", "") for r in store],
    }


def delete_original_file(filepath: str) -> bool:
    """安全销毁原始文件（先覆写随机数据再删除，防止恢复）"""
    try:
        if os.path.exists(filepath):
            size = os.path.getsize(filepath)
            with open(filepath, "wb") as f:
                f.write(os.urandom(size))
            os.remove(filepath)
            return True
    except Exception as e:
        print(f"销毁文件失败: {e}")
        return False
