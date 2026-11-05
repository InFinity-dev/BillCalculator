def post_invoice(client, payload):
    """실제 화면과 동일하게 최종 미리보기 확인 후 정산서를 생성한다."""
    preview = client.post("/invoice/preview", json=payload)
    preview_data = preview.get_json()
    if not preview_data.get("success"):
        return preview
    confirmed = dict(payload)
    confirmed["preview_token"] = preview_data["preview_token"]
    return client.post("/invoice/create", json=confirmed)
