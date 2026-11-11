import httplib2
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build


def create_api(credentials_path):
    credentials = Credentials.from_service_account_file(
        credentials_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)


def column_index(column):
    index = 0
    for letter in column:
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


HEADERS = {
    "published_at": "Дата", "post_title": "Пост", "views": "Просмотры",
    "reactions": "Реакции", "forwards": "Репосты", "comments": "Комментарии",
    "subscribers": "Подписчики", "er": "ER", "err": "ERR", "post_key": "Ключ поста",
    "post_id": "ID поста", "collected_at": "Время сбора", "age_hours": "Возраст, ч",
    "channel": "Канал", "channel_id": "ID канала", "grouped_id": "ID альбома",
    "url": "Ссылка", "note": "Примечание",
}
TECHNICAL = {"post_key", "post_id", "collected_at", "age_hours", "channel_id", "grouped_id"}
WIDTHS = {"published_at": 145, "post_title": 360, "views": 110, "reactions": 100, "forwards": 100, "comments": 125, "subscribers": 125, "er": 90, "err": 90}


def formula(target, field, row):
    columns = target["columns"]
    numerator = "+".join(f"{columns[name]}{row}" for name in ("reactions", "forwards", "comments"))
    divisor = columns["views" if field == "er" else "subscribers"]
    return f"=({numerator})/{divisor}{row}"


def old_array_formula(target, field):
    columns = target["columns"]
    row = target["start_row"]
    numerator = "+".join(f"{columns[name]}{row}:{columns[name]}" for name in ("reactions", "forwards", "comments"))
    divisor = columns["views" if field == "er" else "subscribers"]
    return f"=ARRAYFORMULA(IFERROR(({numerator})/{divisor}{row}:{divisor}))"


def formula_update(sheet_id, target, field, row):
    return {"updateCells": {
        "start": {"sheetId": sheet_id, "rowIndex": row - 1, "columnIndex": column_index(target["columns"][field])},
        "rows": [{"values": [{"userEnteredValue": {"formulaValue": formula(target, field, row)}}]}],
        "fields": "userEnteredValue",
    }}


def setup(api, target):
    metadata = api.spreadsheets().get(spreadsheetId=target["spreadsheet_id"], fields="sheets.properties(sheetId,title,gridProperties)").execute()
    properties = next((item["properties"] for item in metadata["sheets"] if item["properties"]["title"] == target["worksheet"]), None)
    if properties is None:
        raise ValueError(f"Worksheet not found: {target['worksheet']}")
    sheet_id = properties["sheetId"]
    grid = properties["gridProperties"]
    if max(map(column_index, target["columns"].values())) >= grid["columnCount"]:
        raise ValueError("Configured column is outside the existing worksheet grid")
    formula_fields = [field for field in ("er", "err") if field in target["columns"]]
    formula_requests = []
    if formula_fields:
        title = "'" + target["worksheet"].replace("'", "''") + "'"
        fields = ["post_key", *formula_fields]
        response = api.spreadsheets().values().batchGet(
            spreadsheetId=target["spreadsheet_id"],
            ranges=[f"{title}!{target['columns'][field]}{target['start_row']}:{target['columns'][field]}" for field in fields],
            valueRenderOption="FORMULA",
        ).execute()
        values = {field: data.get("values", []) for field, data in zip(fields, response["valueRanges"])}
        for field in formula_fields:
            cells = values[field]
            anchor = cells[0][0] if cells and cells[0] else ""
            normalized = anchor.upper().replace("ЕСЛИОШИБКА", "IFERROR").replace(" ", "") if isinstance(anchor, str) else ""
            replace_array = normalized == old_array_formula(target, field).upper()
            if replace_array:
                formula_requests.append({"updateCells": {
                    "start": {"sheetId": sheet_id, "rowIndex": target["start_row"] - 1, "columnIndex": column_index(target["columns"][field])},
                    "rows": [{"values": [{}]}], "fields": "userEnteredValue",
                }})
            elif normalized.startswith("=ARRAYFORMULA("):
                continue
            for offset, key in enumerate(values["post_key"]):
                if not key or not key[0]:
                    continue
                cell = cells[offset] if offset < len(cells) else []
                if replace_array or not cell or cell[0] == "":
                    formula_requests.append(formula_update(sheet_id, target, field, target["start_row"] + offset))
    requests = [{"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}}]
    for field, column in target["columns"].items():
        index = column_index(column)
        cell_range = {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": index, "endColumnIndex": index + 1}
        cell = {"userEnteredValue": {"stringValue": HEADERS[field]}, "userEnteredFormat": {"backgroundColor": {"red": 0.29, "green": 0.34, "blue": 0.70}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}}
        requests.append({"repeatCell": {"range": cell_range, "cell": cell, "fields": "userEnteredValue,userEnteredFormat"}})
        dimension = {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1}
        properties = {"hiddenByUser": field in TECHNICAL}
        if field not in TECHNICAL:
            properties["pixelSize"] = WIDTHS.get(field, 130)
        requests.append({"updateDimensionProperties": {"range": dimension, "properties": properties, "fields": ",".join(properties)}})
        if field in {"published_at", "er", "err"}:
            data_range = {"sheetId": sheet_id, "startRowIndex": target["start_row"] - 1, "startColumnIndex": index, "endColumnIndex": index + 1}
            number_format = {"type": "DATE_TIME", "pattern": "dd.MM.yyyy HH:mm"} if field == "published_at" else {"type": "PERCENT", "pattern": "0.00%"}
            requests.append({"repeatCell": {"range": data_range, "cell": {"userEnteredFormat": {"numberFormat": number_format}}, "fields": "userEnteredFormat.numberFormat"}})
    requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 36}, "fields": "pixelSize"}})
    requests.extend(formula_requests)
    api.spreadsheets().batchUpdate(spreadsheetId=target["spreadsheet_id"], body={"requests": requests}).execute()


def display_record(record, target):
    result = dict(record)
    age = result.get("age_seconds", round(result["age_hours"] * 3600))
    result["note"] = f"Фактический возраст: {int(age // 3600)} ч {int(age % 3600 // 60)} мин" if age > 86700 else ""
    result["post_title"] = result.get("post_title") or f"Пост №{result['post_id']}"
    published = datetime.fromisoformat(result["published_at"]).astimezone(ZoneInfo(target.get("timezone", "Europe/Moscow"))).replace(tzinfo=None)
    result["published_at"] = (published - datetime(1899, 12, 30)).total_seconds() / 86400
    return result


def inspect(api, target, record, reserved_row=None, minimum_row=None):
    title = "'" + target["worksheet"].replace("'", "''") + "'"
    columns = {field: column for field, column in target["columns"].items() if field not in {"er", "err"}}
    first = target["start_row"]
    response = api.spreadsheets().values().batchGet(
        spreadsheetId=target["spreadsheet_id"],
        ranges=[f"{title}!{col}{first}:{col}" for col in columns.values()],
        valueRenderOption="FORMULA",
    ).execute()
    values = {field: data.get("values", []) for field, data in zip(columns, response["valueRanges"])}
    for field in ("post_key", "url"):
        if field not in values:
            continue
        for offset, cells in enumerate(values[field]):
            if cells and cells[0] == record[field]:
                return first + offset, True
    if reserved_row is not None:
        offset = reserved_row - first
        for cells in values.values():
            if 0 <= offset < len(cells) and cells[offset] and cells[offset][0] != "":
                raise RuntimeError(f"Reserved sheet row {reserved_row} is occupied; check manual edits")
        return reserved_row, False
    return max(first + max((len(cells) for cells in values.values()), default=0), minimum_row or first), False


def write(api, target, record, row):
    record = display_record(record, target)
    metadata = api.spreadsheets().get(
        spreadsheetId=target["spreadsheet_id"],
        fields="sheets.properties(sheetId,title,gridProperties)",
    ).execute()
    properties = next((item["properties"] for item in metadata["sheets"] if item["properties"]["title"] == target["worksheet"]), None)
    if properties is None:
        raise ValueError(f"Worksheet not found: {target['worksheet']}")
    sheet_id = properties["sheetId"]
    requests = []
    grid = properties["gridProperties"]
    if row > grid["rowCount"]:
        requests.append({"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": row - grid["rowCount"]}})
    if max(map(column_index, target["columns"].values())) >= grid["columnCount"]:
        raise ValueError("Configured column is outside the existing worksheet grid")
    formula_fields = [field for field in ("er", "err") if field in target["columns"]]
    occupied = {}
    array_fields = set()
    if formula_fields:
        title = "'" + target["worksheet"].replace("'", "''") + "'"
        ranges = [f"{title}!{target['columns'][field]}{target['start_row']}" for field in formula_fields]
        if row <= grid["rowCount"]:
            ranges.extend(f"{title}!{target['columns'][field]}{row}" for field in formula_fields)
        response = api.spreadsheets().values().batchGet(
            spreadsheetId=target["spreadsheet_id"],
            ranges=ranges,
            valueRenderOption="FORMULA",
        ).execute()
        results = response["valueRanges"]
        for field, data in zip(formula_fields, results):
            cells = data.get("values", [])
            value = cells[0][0] if cells and cells[0] else ""
            if isinstance(value, str) and value.upper().replace(" ", "").startswith("=ARRAYFORMULA("):
                array_fields.add(field)
        if row <= grid["rowCount"]:
            occupied = {field: data.get("values", []) for field, data in zip(formula_fields, results[len(formula_fields):])}
    for field, column in target["columns"].items():
        if field in {"er", "err"}:
            cells = occupied.get(field, [])
            if field not in array_fields and (not cells or not cells[0] or cells[0][0] == ""):
                requests.append(formula_update(sheet_id, target, field, row))
            continue
        value = record[field]
        cell = {"userEnteredValue": {"numberValue": value} if isinstance(value, (int, float)) else {"stringValue": str(value)}}
        fields = "userEnteredValue"
        if field == "views":
            cell["note"] = record["note"]
            fields += ",note"
        if field == "post_title":
            cell["textFormatRuns"] = [{"startIndex": 0, "format": {"link": {"uri": record["url"]}}}]
            fields += ",textFormatRuns"
        requests.append({"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row - 1, "columnIndex": column_index(column)},
            "rows": [{"values": [cell]}],
            "fields": fields,
        }})
    api.spreadsheets().batchUpdate(
        spreadsheetId=target["spreadsheet_id"], body={"requests": requests}
    ).execute()
