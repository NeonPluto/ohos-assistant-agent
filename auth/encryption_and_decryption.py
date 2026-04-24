"""
安全加解密工具模块。

能力说明：
1. 支持国密 SM2（依赖 gmssl，可选安装）
2. 支持对称加密（AES、SM4）
3. 支持非对称加密（RSA）
4. 统一 encrypt/decrypt 接口，支持多种算法与密钥长度校验
"""

from __future__ import annotations

import base64
import os
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class SecurityEncryption:
    """安全加解密类，统一封装对称/非对称/国密 SM2 能力。"""

    # 支持的对称算法及对应合法密钥长度（单位：字节）
    _SYMMETRIC_KEY_LENGTHS = {
        "AES": {16, 24, 32},
        "SM4": {16},
    }
    # 支持的 RSA 密钥长度（单位：bit）
    _RSA_KEY_SIZES = {2048, 3072, 4096}

    @classmethod
    def generate_symmetric_key(cls, algorithm: str = "AES", key_length: int = 32) -> bytes:
        """
        生成对称密钥。

        :param algorithm: 算法名称，支持 AES/SM4
        :param key_length: 密钥长度（字节）
        :return: 原始二进制密钥
        """
        algo = algorithm.upper()
        cls._validate_symmetric_key_length(algo, key_length)
        return os.urandom(key_length)

    @classmethod
    def generate_rsa_key_pair(
        cls, key_size: int = 2048
    ) -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
        """
        生成 RSA 密钥对。

        :param key_size: 密钥位数，支持 2048/3072/4096
        :return: (私钥对象, 公钥对象)
        """
        if key_size not in cls._RSA_KEY_SIZES:
            raise ValueError(f"RSA 不支持 key_size={key_size}，可选: {sorted(cls._RSA_KEY_SIZES)}")
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
            backend=default_backend(),
        )
        return private_key, private_key.public_key()

    @classmethod
    def encrypt(
        cls,
        plaintext: str,
        algorithm: str,
        key: Any,
        **kwargs: Any,
    ) -> str:
        """
        统一加密入口。

        :param plaintext: 明文字符串
        :param algorithm: 算法名称（AES/SM4/RSA/SM2）
        :param key: 算法对应密钥对象
        :param kwargs: 扩展参数（如 iv、public_key）
        :return: Base64 编码后的密文字符串
        """
        algo = algorithm.upper()
        data = plaintext.encode("utf-8")

        if algo in {"AES", "SM4"}:
            iv = kwargs.get("iv") or os.urandom(16)
            cipher_text = cls._encrypt_symmetric(algo, data, key, iv)
            # 对称加密返回 iv + 密文，便于后续解密直接使用
            return base64.b64encode(iv + cipher_text).decode("utf-8")
        if algo == "RSA":
            cipher_text = cls._encrypt_rsa(data, kwargs.get("public_key") or key)
            return base64.b64encode(cipher_text).decode("utf-8")
        if algo == "SM2":
            cipher_text = cls._encrypt_sm2(data, key)
            return base64.b64encode(cipher_text).decode("utf-8")
        raise ValueError(f"不支持的加密算法: {algorithm}")

    @classmethod
    def decrypt(
        cls,
        ciphertext: str,
        algorithm: str,
        key: Any,
        **kwargs: Any,
    ) -> str:
        """
        统一解密入口。

        :param ciphertext: Base64 编码密文
        :param algorithm: 算法名称（AES/SM4/RSA/SM2）
        :param key: 算法对应密钥对象
        :param kwargs: 扩展参数（如 private_key）
        :return: 解密后的明文字符串（UTF-8）
        """
        algo = algorithm.upper()
        raw_cipher = base64.b64decode(ciphertext.encode("utf-8"))

        if algo in {"AES", "SM4"}:
            if len(raw_cipher) <= 16:
                raise ValueError("对称密文格式非法：至少包含 16 字节 IV 和有效密文")
            iv = raw_cipher[:16]
            cipher_data = raw_cipher[16:]
            plain_data = cls._decrypt_symmetric(algo, cipher_data, key, iv)
            return plain_data.decode("utf-8")
        if algo == "RSA":
            plain_data = cls._decrypt_rsa(raw_cipher, kwargs.get("private_key") or key)
            return plain_data.decode("utf-8")
        if algo == "SM2":
            plain_data = cls._decrypt_sm2(raw_cipher, key)
            return plain_data.decode("utf-8")
        raise ValueError(f"不支持的解密算法: {algorithm}")

    @classmethod
    def _validate_symmetric_key_length(cls, algorithm: str, key_length: int) -> None:
        """校验对称算法密钥长度是否合法。"""
        allowed = cls._SYMMETRIC_KEY_LENGTHS.get(algorithm)
        if not allowed:
            raise ValueError(f"不支持的对称算法: {algorithm}")
        if key_length not in allowed:
            raise ValueError(
                f"{algorithm} 不支持 key_length={key_length}，可选: {sorted(allowed)}"
            )

    @classmethod
    def _encrypt_symmetric(cls, algorithm: str, data: bytes, key: bytes, iv: bytes) -> bytes:
        """执行对称加密（CBC + PKCS7）。"""
        cls._validate_symmetric_key_length(algorithm, len(key))
        if len(iv) != 16:
            raise ValueError("对称加密 IV 长度必须为 16 字节")

        if algorithm == "AES":
            algo_obj = algorithms.AES(key)
        else:
            algo_obj = algorithms.SM4(key)

        padder = padding.PKCS7(algo_obj.block_size).padder()
        padded = padder.update(data) + padder.finalize()
        cipher = Cipher(algo_obj, modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    @classmethod
    def _decrypt_symmetric(cls, algorithm: str, data: bytes, key: bytes, iv: bytes) -> bytes:
        """执行对称解密（CBC + PKCS7）。"""
        cls._validate_symmetric_key_length(algorithm, len(key))
        if len(iv) != 16:
            raise ValueError("对称解密 IV 长度必须为 16 字节")

        if algorithm == "AES":
            algo_obj = algorithms.AES(key)
        else:
            algo_obj = algorithms.SM4(key)

        cipher = Cipher(algo_obj, modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(data) + decryptor.finalize()
        unpadder = padding.PKCS7(algo_obj.block_size).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

    @staticmethod
    def _encrypt_rsa(data: bytes, public_key: rsa.RSAPublicKey) -> bytes:
        """使用 RSA 公钥进行 OAEP 加密。"""
        return public_key.encrypt(
            data,
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

    @staticmethod
    def _decrypt_rsa(ciphertext: bytes, private_key: rsa.RSAPrivateKey) -> bytes:
        """使用 RSA 私钥进行 OAEP 解密。"""
        return private_key.decrypt(
            ciphertext,
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

    @staticmethod
    def _encrypt_sm2(data: bytes, public_key_hex: str) -> bytes:
        """
        使用 SM2 公钥加密。

        说明：SM2 依赖 gmssl 包；若未安装会给出明确错误提示。
        """
        try:
            from gmssl import sm2  # type: ignore
        except ImportError as exc:
            raise ImportError("使用 SM2 需要先安装 gmssl: pip install gmssl") from exc

        cryptor = sm2.CryptSM2(public_key=public_key_hex, private_key="")
        return cryptor.encrypt(data)

    @staticmethod
    def _decrypt_sm2(ciphertext: bytes, private_key_hex: str) -> bytes:
        """
        使用 SM2 私钥解密。

        说明：SM2 依赖 gmssl 包；若未安装会给出明确错误提示。
        """
        try:
            from gmssl import sm2  # type: ignore
        except ImportError as exc:
            raise ImportError("使用 SM2 需要先安装 gmssl: pip install gmssl") from exc

        cryptor = sm2.CryptSM2(public_key="", private_key=private_key_hex)
        return cryptor.decrypt(ciphertext)