import base64
import hashlib
import hmac
from http import HTTPStatus
from io import BytesIO
from typing import Optional

from embit import bech32, compact
from fastapi import Request
from fastapi.param_functions import Query
from starlette.exceptions import HTTPException

import secrets
from http import HTTPStatus

from fastapi.params import Depends, Query
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse

from lnbits.core.services import create_invoice
from lnbits.core.views.api import pay_invoice

from lnurl import Lnurl, LnurlWithdrawResponse
from lnurl import encode as lnurl_encode  # type: ignore
from lnurl.types import LnurlPayMetadata  # type: ignore

from . import boltcards_ext
from .crud import (
    create_hit,
    get_card,
    get_card_by_otp,
    get_card_by_uid,
    get_hit,
    get_hits_today,
    update_card,
    update_card_counter,
    update_card_otp,
)
from .models import CreateCardData
from .nxp424 import decryptSUN, getSunMAC

###############LNURLWITHDRAW#################

# /boltcards/api/v1/scan?p=00000000000000000000000000000000&c=0000000000000000
@boltcards_ext.get("/api/v1/scan/{card_uid}")
async def api_scan(p, c, request: Request, card_uid: str = None):
    # some wallets send everything as lower case, no bueno
    p = p.upper()
    c = c.upper()
    card = None
    counter = b""
    try:
        card = await get_card_by_uid(card_uid)
        card_uid, counter = decryptSUN(bytes.fromhex(p), bytes.fromhex(card.k1))

        if card.uid.upper() != card_uid.hex().upper():
            return {"status": "ERROR", "reason": "Card UID mis-match."}
    except:
        return {"status": "ERROR", "reason": "Error decrypting card."}

    if card == None:
        return {"status": "ERROR", "reason": "Unknown card."}

    if c != getSunMAC(card_uid, counter, bytes.fromhex(card.k2)).hex().upper():
        return {"status": "ERROR", "reason": "CMAC does not check."}

    ctr_int = int.from_bytes(counter, "little")
    
    if ctr_int <= card.counter:
        return {"status": "ERROR", "reason": "This link is already used."}

    await update_card_counter(ctr_int, card.id)

    # gathering some info for hit record
    ip = request.client.host
    if "x-real-ip" in request.headers:
        ip = request.headers["x-real-ip"]
    elif "x-forwarded-for" in request.headers:
        ip = request.headers["x-forwarded-for"]

    agent = request.headers["user-agent"] if "user-agent" in request.headers else ""
    todays_hits = await get_hits_today(card.id)

    hits_amount = 0
    for hit in todays_hits:
        hits_amount = hits_amount + hit.amount
    if (hits_amount + card.tx_limit) > card.daily_limit:
        return {"status": "ERROR", "reason": "Max daily liit spent."}
    hit = await create_hit(card.id, ip, agent, card.counter, ctr_int)
    lnurlpay = lnurl_encode(request.url_for("boltcards.lnurlp_response", hit_id=hit.id))
    return {
        "tag": "withdrawRequest",
        "callback": request.url_for(
            "boltcards.lnurl_callback"
        ),
        "k1": hit.id,
        "minWithdrawable": 1 * 1000,
        "maxWithdrawable": card.tx_limit * 1000,
        "defaultDescription": f"Boltcard (refund address {lnurlpay})",
    }

@boltcards_ext.get(
    "/api/v1/lnurl/cb/{hitid}",
    status_code=HTTPStatus.OK,
    name="boltcards.lnurl_callback",
)
async def lnurl_callback(
    request: Request,
    pr: str = Query(None),
    k1: str = Query(None),
):
    hit = await get_hit(k1) 
    card = await get_card(hit.id) 
    if not hit:
        return {"status": "ERROR", "reason": f"LNURL-pay record not found."}

    if pr:
        if hit.id != k1:
            return {"status": "ERROR", "reason": "Bad K1"}
        if hit.spent:
            return {"status": "ERROR", "reason": f"Payment already claimed"}
        hit = await spend_hit(hit.id)
        if not hit:
            return {"status": "ERROR", "reason": f"Payment failed"}
        await pay_invoice(
            wallet_id=card.wallet,
            payment_request=pr,
            max_sat=card.tx_limit / 1000,
            extra={"tag": "boltcard"},
        )
        return {"status": "OK"}
    else:
        return {"status": "ERROR", "reason": f"Payment failed"}


# /boltcards/api/v1/auth?a=00000000000000000000000000000000
@boltcards_ext.get("/api/v1/auth")
async def api_auth(a, request: Request):
    if a == "00000000000000000000000000000000":
        response = {"k0": "0" * 32, "k1": "1" * 32, "k2": "2" * 32}
        return response

    card = await get_card_by_otp(a)

    if not card:
        raise HTTPException(
            detail="Card does not exist.", status_code=HTTPStatus.NOT_FOUND
        )

    new_otp = secrets.token_hex(16)
    print(card.otp)
    print(new_otp)
    await update_card_otp(new_otp, card.id)

    response = {"k0": card.k0, "k1": card.k1, "k2": card.k2}

    return response

###############LNURLPAY REFUNDS#################

@boltcards_ext.get(
    "/api/v1/lnurlp/{hit_id}",
    response_class=HTMLResponse,
    name="boltcards.lnurlp_response",
)
async def lnurlp_response(req: Request, hit_id: str = Query(None)):
    hit = await get_hit(hit_id) 
    if not hit:
        return {"status": "ERROR", "reason": f"LNURL-pay record not found."}
    payResponse = {
        "tag": "payRequest",
        "callback": req.url_for("boltcards.lnurlp_callback", hit_id=hit_id),
        "metadata": LnurlPayMetadata(json.dumps([["text/plain", "Refund"]])),
        "minSendable": math.ceil(link.min_bet * 1) * 1000,
        "maxSendable": round(link.max_bet * 1) * 1000,
    }
    return json.dumps(payResponse)


@boltcards_ext.get(
    "/api/v1/lnurlp/cb/{hit_id}",
    response_class=HTMLResponse,
    name="boltcards.lnurlp_callback",
)
async def lnurlp_callback(
    req: Request, hit_id: str = Query(None), amount: str = Query(None)
):
    hit = await get_hit(hit_id) 
    if not hit:
        return {"status": "ERROR", "reason": f"LNURL-pay record not found."}

    payment_hash, payment_request = await create_invoice(
        wallet_id=link.wallet,
        amount=int(amount / 1000),
        memo=f"Refund {hit_id}",
        unhashed_description=LnurlPayMetadata(json.dumps([["text/plain", hit_id]])).encode("utf-8"),
        extra={"refund": hit_id},
    )

    payResponse = {"pr": payment_request, "successAction": success_action, "routes": []}

    return json.dumps(payResponse)




