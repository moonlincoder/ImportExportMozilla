#!/usr/bin/env python3

"""
fork of https://github.com/louisabraham/ffpass

The MIT License (MIT)
Copyright (c) 2018 Louis Abraham <louis.abraham@yahoo.fr>

ffpass can import and export passwords from Firefox Quantum.
If you found this code useful, add a star on <https://github.com/louisabraham/ffpass>!
"""

import sys
from base64 import b64decode, b64encode
from hashlib import sha1
import hmac
import json
from pathlib import Path
import csv
import secrets
from getpass import getpass
from uuid import uuid4
from datetime import datetime
import configparser
from urllib.parse import urlparse
import sqlite3
import os.path

from pyasn1.codec.der.decoder import decode as der_decode
from pyasn1.codec.der.encoder import encode as der_encode
from pyasn1.type.univ import Sequence, OctetString, ObjectIdentifier
from Crypto.Cipher import DES3

MAGIC1 = b"\xf8\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01"
MAGIC2 = (1, 2, 840, 113549, 3, 7)


class NoDatabase(Exception):
    pass


class WrongPassword(Exception):
    pass


def getKey(directory: Path, masterPassword=""):
    dbfile: Path = directory / "key4.db"
    if not dbfile.exists():
        raise NoDatabase()
    # firefox 58.0.2 / NSS 3.35 with key4.db in SQLite
    conn = sqlite3.connect(dbfile.as_posix())
    c = conn.cursor()
    # first check password
    c.execute("SELECT item1,item2 FROM metadata WHERE id = 'password';")
    row = next(c)
    globalSalt = row[0]  # item1
    item2 = row[1]
    decodedItem2, _ = der_decode(item2)
    entrySalt = decodedItem2[0][1][0].asOctets()
    cipherT = decodedItem2[1].asOctets()
    clearText = decrypt3DES(
        globalSalt, masterPassword, entrySalt, cipherT
    )  # usual Mozilla PBE
    if clearText != b"password-check\x02\x02":
        raise WrongPassword()
    # decrypt 3des key to decrypt "logins.json" content

    c.execute("SELECT a11,a102 FROM nssPrivate;")
    for row in c:
        if row[1] == MAGIC1:
            break
    a11 = row[0]
    assert (row[1] == MAGIC1), "The Firefox database appears to be broken. Try to add a password to rebuild it."
    decodedA11, _ = der_decode(a11)
    entrySalt = decodedA11[0][1][0].asOctets()
    cipherT = decodedA11[1].asOctets()
    key = decrypt3DES(globalSalt, masterPassword, entrySalt, cipherT)
    return key[:24]


def PKCS7pad(b):
    l = (-len(b) - 1) % 8 + 1
    return b + bytes([l] * l)


def PKCS7unpad(b):
    return b[: -b[-1]]


def decrypt3DES(globalSalt, masterPassword, entrySalt, encryptedData):
    hp = sha1(globalSalt + masterPassword.encode()).digest()
    pes = entrySalt + b"\x00" * (20 - len(entrySalt))
    chp = sha1(hp + entrySalt).digest()
    k1 = hmac.new(chp, pes + entrySalt, sha1).digest()
    tk = hmac.new(chp, pes, sha1).digest()
    k2 = hmac.new(chp, tk + entrySalt, sha1).digest()
    k = k1 + k2
    iv = k[-8:]
    key = k[:24]
    return DES3.new(key, DES3.MODE_CBC, iv).decrypt(encryptedData)


def decodeLoginData(key, data):
    # first base64 decoding, then ASN1DERdecode
    asn1data, _ = der_decode(b64decode(data))
    assert asn1data[0].asOctets() == MAGIC1
    assert asn1data[1][0].asTuple() == MAGIC2
    iv = asn1data[1][1].asOctets()
    ciphertext = asn1data[2].asOctets()
    des = DES3.new(key, DES3.MODE_CBC, iv)
    return PKCS7unpad(des.decrypt(ciphertext)).decode()


def encodeLoginData(key, data):
    iv = secrets.token_bytes(8)
    des = DES3.new(key, DES3.MODE_CBC, iv)
    ciphertext = des.encrypt(PKCS7pad(data.encode()))
    asn1data = Sequence()
    asn1data[0] = OctetString(MAGIC1)
    asn1data[1] = Sequence()
    asn1data[1][0] = ObjectIdentifier(MAGIC2)
    asn1data[1][1] = OctetString(iv)
    asn1data[2] = OctetString(ciphertext)
    return b64encode(der_encode(asn1data)).decode()


def getJsonLogins(directory):
    with open(directory / "logins.json", "r") as loginf:
        jsonLogins = json.load(loginf)
    return jsonLogins


def dumpJsonLogins(directory, jsonLogins):
    with open(directory / "logins.json", "w") as loginf:
        json.dump(jsonLogins, loginf, separators=",:")


def exportLogins(key, jsonLogins, fields):
    """ fields: id, hostname, login, password, timeCreated, timeLastUsed, timePasswordChanged, timesUsed """
    if "logins" not in jsonLogins:
        print("error: no 'logins' key in logins.json", file=sys.stderr)
        return []
    logins = []
    for row in jsonLogins["logins"]:
        blank = {}
        for field in fields:
            if field == 'password':
                blank['password'] = decodeLoginData(key, row["encryptedPassword"])
            elif field == 'login':
                blank['login'] = decodeLoginData(key, row["encryptedUsername"])
            else:
                blank[field] = row[field]
        logins.append(blank)
    return logins


def lower_header(from_file):
    it = iter(from_file)
    yield next(it).lower()
    yield from it


def readCSV(from_file):
    logins = []
    reader = csv.DictReader(lower_header(from_file))
    for row in reader:
        logins.append((
            rawURL(row["url"]),
            row["username"],
            row["password"],
        ))
    return logins


def rawURL(url):
    p = urlparse(url)
    return type(p)(*p[:2], *[""] * 4).geturl()


def addNewLogin(key, jsonLogins, login):
    nextId = jsonLogins["nextId"]
    timestamp = int(datetime.now().timestamp() * 1000)

    entry = {
        "id": nextId,
        "hostname": login['hostname'],
        "httpRealm": None,
        "formSubmitURL": "",
        "usernameField": "",
        "passwordField": "",
        "encryptedUsername": encodeLoginData(key, login['login']),
        "encryptedPassword": encodeLoginData(key, login['password']),
        "guid": "{%s}" % uuid4(),
        "encType": 1,
        "timeCreated": timestamp,
        "timeLastUsed": timestamp,
        "timePasswordChanged": timestamp,
        "timesUsed": 0,
    }
    jsonLogins["logins"].append(entry)
    jsonLogins["nextId"] += 1


def delNewLogin(key, jsonLogins, login):
    replace_index = getLogin(
        login['hostname'],
        login["login"],
        login['password'],
        key, jsonLogins
    )
    del jsonLogins['logins'][replace_index]
    jsonLogins["nextId"] -= 1


def getLogin(url, login, password, key, jsonLogins):
    logins = exportLogins(key, jsonLogins, ['hostname', 'login', 'password'])
    index = logins.index({
        'hostname': url,
        'login': login,
        'password': password
    })
    return index



def findProfiles():
    dirs = {
        "darwin": "~/Library/Application Support/Firefox/Profiles",
        "linux": "~/.mozilla/firefox/Profiles",
        "win32": os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles"),
        "cygwin": os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles"),
    }
    if sys.platform in dirs:
        path = Path(dirs[sys.platform]).expanduser()
        out_profiles = []
        for profile in path.iterdir():
            if profile.is_dir():
                out_profiles.append(profile)

        return out_profiles
    else:
        print("Automatic profile selection not supported for platform", sys.platform)


def askpass(directory):
    password = ""
    while True:
        try:
            key = getKey(directory, password)
        except WrongPassword:
            password = getpass("Master Password:")
        else:
            break
    return key
