import asyncio
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

import main


def reset_extract_queue():
    main.extract_queue.clear()
    main.extract_queue_items.clear()
    main.extract_queue_worker = None


def test_clean_url_drops_query_and_fragment():
    raw = "https://www.xiaohongshu.com/goods-detail/688c1495b1b9df00012f1a34?xsec_token=abc&share_id=1#frag"
    assert main.clean_url(raw) == "https://www.xiaohongshu.com/goods-detail/688c1495b1b9df00012f1a34"


def test_clean_url_extracts_from_text():
    raw = "看看这个 https://www.xiaohongshu.com/goods-detail/abc?xsec_token=xxx，复制链接"
    assert main.clean_url(raw) == "https://www.xiaohongshu.com/goods-detail/abc"


def test_clean_endpoint_without_feishu():
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": ""}):
        data = main.clean_only(main.SubmitRequest(url="https://www.xiaohongshu.com/goods-detail/abc?x=1"), None)
    assert data["clean_url"] == "https://www.xiaohongshu.com/goods-detail/abc"


def test_submit_uses_mocked_feishu():
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": ""}), patch.object(main, "create_feishu_record", return_value={"code": 0, "data": {"record": {"record_id": "rec_test"}}}):
        data = main.submit(main.SubmitRequest(url="https://www.xiaohongshu.com/goods-detail/abc?x=1"), None)
    assert data["clean_url"] == "https://www.xiaohongshu.com/goods-detail/abc"
    assert data["feishu"]["record_id"] == "rec_test"
    assert data["action"] == "clean"


def test_detect_xhs_input_type_recognizes_product_and_note_urls():
    assert main.detect_xhs_input_type("https://www.xiaohongshu.com/goods-detail/abc?x=1") == "product"
    assert main.detect_xhs_input_type("https://www.xiaohongshu.com/explore/688c1495b1b9df00012f1a34?xsec_token=1") == "note"


def test_detect_xhs_input_type_resolves_short_note_link():
    resp = Mock()
    resp.url = "https://www.xiaohongshu.com/explore/688c1495b1b9df00012f1a34?xsec_token=1"
    with patch("main.requests.get", return_value=resp):
        assert main.detect_xhs_input_type("http://xhslink.com/o/demo") == "note"


def test_detect_xhs_input_type_resolves_xhslink_cn_note_link():
    resp = Mock()
    resp.url = "https://www.xiaohongshu.com/discovery/item/69a9192900000000230386ea?xsec_token=1"
    with patch("main.requests.get", return_value=resp):
        assert main.detect_xhs_input_type("http://xhslink.cn/o/ACpZSDGAOqA") == "note"


def test_clean_url_resolves_xhslink_cn_short_link():
    resp = Mock()
    resp.url = "https://www.xiaohongshu.com/discovery/item/69a9192900000000230386ea?xsec_token=abc"
    with patch("main.requests.get", return_value=resp):
        assert main.clean_url("http://xhslink.cn/o/ACpZSDGAOqA") == (
            "https://www.xiaohongshu.com/discovery/item/69a9192900000000230386ea"
        )


def test_submit_routes_note_to_extract_table():
    note = {"success": True, "title": "T", "description": "D", "tags": ["tag"], "image_urls": ["https://img/a.jpg"]}
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": "", "FEISHU_EXTRACT_IMAGE_FIELD": "图片链接"}), \
        patch.object(main, "extract_xhs_note", new=AsyncMock(return_value=note)), \
        patch.object(main, "create_extract_record", return_value={"record_id": "rec_note"}) as mocked_create:
        data = main.submit(main.SubmitRequest(url="https://www.xiaohongshu.com/explore/688c1495b1b9df00012f1a34?x=1"), None)

    assert data["action"] == "extract_note"
    assert data["detected_type"] == "note"
    assert data["feishu"]["record_id"] == "rec_note"
    assert mocked_create.call_args.args[0] == {"标题": "T", "正文": "D", "标签": "#tag", "图片链接": "https://img/a.jpg"}


def test_extract_note_submit_routes_product_to_clean_table():
    reset_extract_queue()
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": ""}), \
        patch.object(main, "create_feishu_record", return_value={"code": 0, "data": {"record": {"record_id": "rec_clean"}}}):
        data = asyncio.run(main.extract_note_submit(main.XhsExtractSubmitRequest(url="https://www.xiaohongshu.com/goods-detail/abc?x=1"), None, None))

    assert data["action"] == "clean"
    assert data["detected_type"] == "product"
    assert data["clean_url"] == "https://www.xiaohongshu.com/goods-detail/abc"
    assert data["feishu"]["record_id"] == "rec_clean"
    assert not main.extract_queue


def test_frontend_login_returns_token_and_protects_api():
    env = {"FRONTEND_PASSWORD": "secret", "FRONTEND_SESSION_SECRET": "signing-secret"}

    with patch.dict(os.environ, env):
        token = main.login(main.LoginRequest(password="secret"))["token"]
        main.verify_api_key(x_frontend_token=token)
        with pytest.raises(HTTPException):
            main.verify_api_key()


def test_frontend_login_rejects_wrong_password():
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "secret"}):
        with pytest.raises(HTTPException):
            main.login(main.LoginRequest(password="wrong"))


def test_build_note_copy_body_appends_tags_after_body():
    assert main.build_note_copy_body("正文内容\n", "#标签1#标签2") == "正文内容\n\n#标签1#标签2"


def test_normalize_note_record_extracts_note_fields_and_images():
    record = {
        "record_id": "rec_note",
        "fields": {
            "标题": "测试标题",
            "正文": [{"text": "第一段"}, {"text": "第二段"}],
            "标签": "#标签1#标签2",
            "是否拉取": "",
            "笔记类别": "穿搭",
            "图片": [{"file_token": "token1", "name": "one.jpg", "type": "image/jpeg"}],
        },
    }

    note = main.normalize_note_record(record, include_detail=True)

    assert note["record_id"] == "rec_note"
    assert note["title"] == "测试标题"
    assert note["copy_body"] == "第一段第二段\n\n#标签1#标签2"
    assert note["category"] == "穿搭"
    assert note["fetched"] == "否"
    assert note["images"] == [{"index": 0, "name": "one.jpg", "file_token": "token1", "size": None, "type": "image/jpeg"}]


def test_filter_note_summaries_filters_category_and_fetched():
    records = [
        {"record_id": "rec1", "fields": {"标题": "A", "笔记类别": "穿搭", "是否拉取": "否"}},
        {"record_id": "rec2", "fields": {"标题": "B", "笔记类别": "美食", "是否拉取": "是"}},
    ]

    with patch.object(main, "list_notes_records", return_value=records):
        notes = main.filter_note_summaries("穿搭", "否")

    assert [note["record_id"] for note in notes] == ["rec1"]


def test_build_bitable_attachment_extra_uses_image_field_id():
    env = {"FEISHU_NOTES_TABLE_ID": "tbl_test", "FEISHU_NOTES_IMAGE_FIELD": "图片"}
    fields = [{"field_id": "fld_image", "field_name": "图片"}]

    main.notes_field_cache.image_field_id = None
    with patch.dict(os.environ, env), patch.object(main, "list_notes_fields", return_value=fields):
        extra = main.build_bitable_attachment_extra("rec_note", "file_token")
    main.notes_field_cache.image_field_id = None

    assert extra == '{"bitablePerm":{"tableId":"tbl_test","attachments":{"fld_image":{"rec_note":["file_token"]}}}}'


def test_upload_bitable_attachment_media_uses_drive_upload_all():
    resp = Mock(status_code=200)
    resp.json.return_value = {"code": 0, "data": {"file_token": "file_tok"}}

    with patch.object(main, "get_feishu_tenant_access_token", return_value="tenant"), patch("main.requests.post", return_value=resp) as mocked_post:
        token = main.upload_bitable_attachment_media("app_test", "image.jpg", b"abc", "upload test")

    assert token == "file_tok"
    assert mocked_post.call_args.args[0] == "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"
    assert mocked_post.call_args.kwargs["data"] == {
        "file_name": "image.jpg",
        "parent_type": "bitable_file",
        "parent_node": "app_test",
        "size": "3",
    }
    assert mocked_post.call_args.kwargs["files"]["file"] == ("image.jpg", b"abc", "application/octet-stream")


def test_patch_base_attachment_cell_uses_unwrapped_record_map():
    resp = Mock(status_code=200)
    resp.json.return_value = {"code": 0, "data": {}}
    attachments = [{"deprecated_set_attachment": True, "file_token": "tok", "name": "image.jpg"}]

    with patch.object(main, "get_feishu_tenant_access_token", return_value="tenant"), patch("main.requests.patch", return_value=resp) as mocked_patch:
        main.patch_base_attachment_cell("app_test", "tbl_test", "rec_test", "图片", attachments, "patch test")

    assert mocked_patch.call_args.args[0] == "https://open.feishu.cn/open-apis/base/v3/bases/app_test/tables/tbl_test/records/rec_test"
    assert mocked_patch.call_args.kwargs["json"] == {"图片": attachments}
    assert "fields" not in mocked_patch.call_args.kwargs["json"]


def test_update_note_fetched_writes_yes_single_select():
    resp = Mock(status_code=200)
    resp.json.return_value = {"code": 0, "data": {}}
    env = {
        "FEISHU_NOTES_BITABLE_APP_TOKEN": "base_test",
        "FEISHU_NOTES_TABLE_ID": "tbl_test",
        "FEISHU_NOTES_FETCHED_FIELD": "是否拉取",
        "FEISHU_NOTES_FETCHED_YES_VALUE": "是",
    }

    with patch.dict(os.environ, env), patch.object(main, "get_feishu_tenant_access_token", return_value="tenant"), patch("main.requests.put", return_value=resp) as mocked_put:
        main.update_note_fetched("rec_note")

    assert mocked_put.call_args.kwargs["json"] == {"fields": {"是否拉取": "是"}}



def test_build_xhs_note_fields_formats_body_tags_category_and_fetched():
    note = {"title": "标题", "description": "正文", "tags": ["教资", "#笔试", "教资"]}
    with patch.dict(os.environ, {
        "FEISHU_NOTES_TITLE_FIELD": "标题",
        "FEISHU_NOTES_BODY_FIELD": "正文",
        "FEISHU_NOTES_TAGS_FIELD": "标签",
        "FEISHU_NOTES_CATEGORY_FIELD": "笔记类别",
        "FEISHU_NOTES_FETCHED_FIELD": "是否拉取",
    }):
        fields = main.build_xhs_note_fields(note, category="教资", fetched="否")

    assert fields == {"标题": "标题", "正文": "正文", "标签": "#教资 #笔试", "笔记类别": "教资", "是否拉取": "否"}


def test_extract_note_api_response_shape_with_mocked_playwright():
    note = {"success": True, "title": "T", "description": "D", "tags": ["tag"], "image_urls": ["https://img/a.jpg"]}
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": ""}), patch.object(main, "extract_xhs_note", new=AsyncMock(return_value=note)) as mocked:
        data = asyncio.run(main.extract_note(main.XhsExtractRequest(url="http://xhslink.com/o/demo", timeout_ms=5000, settle_ms=500, scroll_times=0), None, None))

    assert data == {"ok": True, "note": note}
    assert mocked.await_args.args[0] == "http://xhslink.com/o/demo"
    assert mocked.await_args.args[1].timeout_ms == 5000


def test_extract_note_invalid_input_maps_to_400():
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": ""}), patch.object(main, "extract_xhs_note", new=AsyncMock(side_effect=ValueError("No http(s) URL found in input"))):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.extract_note(main.XhsExtractRequest(url="not-a-url"), None, None))

    assert exc.value.status_code == 400
    assert "No http" in exc.value.detail


def test_run_extract_submit_writes_image_urls_to_extract_record():
    image_urls = ["https://img/a.jpg", "https://img/b.jpg", "https://img/c.jpg"]
    note = {"success": True, "title": "T", "description": "D", "tags": ["tag"], "image_urls": image_urls}
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": "", "FEISHU_EXTRACT_IMAGE_FIELD": "图片链接"}), \
        patch.object(main, "extract_xhs_note", new=AsyncMock(return_value=note)), \
        patch.object(main, "create_extract_record", return_value={"record_id": "rec1"}) as mocked_create, \
        patch.object(main, "upload_xhs_images_to_extract", return_value=[{"ok": True, "file_token": "tok"}]) as mocked_upload:
        data = asyncio.run(main.run_extract_submit(main.XhsExtractSubmitRequest(url="http://xhslink.com/o/demo")))

    assert data["ok"] is True
    assert data["feishu"]["record_id"] == "rec1"
    assert data["images"] == {"urls": image_urls}
    assert mocked_create.call_args.args[0] == {"标题": "T", "正文": "D", "标签": "#tag", "图片链接": "https://img/a.jpg\nhttps://img/b.jpg\nhttps://img/c.jpg"}
    mocked_upload.assert_not_called()


def test_extract_note_submit_enqueues_and_returns_position():
    reset_extract_queue()

    def fake_create_task(coro):
        coro.close()
        return Mock(done=Mock(return_value=False))

    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": ""}), patch.object(main.asyncio, "create_task", side_effect=fake_create_task) as mocked_create_task:
        data = asyncio.run(main.extract_note_submit(main.XhsExtractSubmitRequest(url="http://xhslink.com/o/demo"), None, None))

    assert data["ok"] is True
    assert data["queued"] is True
    assert data["position"] == 1
    assert data["ahead"] == 0
    assert len(main.extract_queue) == 1
    mocked_create_task.assert_called_once()
    reset_extract_queue()


def test_extract_note_submit_reports_later_queue_position():
    reset_extract_queue()
    main.extract_queue.append(main.ExtractQueueItem(task_id="running", body=main.XhsExtractSubmitRequest(url="http://xhslink.com/o/running"), submitted_at=1, status="running"))
    main.extract_queue_worker = Mock(done=Mock(return_value=False))

    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": ""}):
        data = asyncio.run(main.extract_note_submit(main.XhsExtractSubmitRequest(url="http://xhslink.com/o/demo"), None, None))

    assert data["position"] == 2
    assert data["ahead"] == 1
    assert "前方还有 1 个任务" in data["message"]
    reset_extract_queue()


def test_process_extract_queue_reuses_one_browser_for_queued_items():
    reset_extract_queue()
    item1 = main.ExtractQueueItem(task_id="1", body=main.XhsExtractSubmitRequest(url="http://xhslink.com/o/one"), submitted_at=1)
    item2 = main.ExtractQueueItem(task_id="2", body=main.XhsExtractSubmitRequest(url="http://xhslink.com/o/two"), submitted_at=2)
    main.extract_queue.extend([item1, item2])
    run_mock = AsyncMock(side_effect=[{"ok": True, "n": 1}, {"ok": True, "n": 2}])

    with patch.object(main, "launch_xhs_browser", new=AsyncMock(return_value=("playwright", "browser"))) as launch_mock, \
        patch.object(main, "close_xhs_browser", new=AsyncMock()) as close_mock, \
        patch.object(main, "run_extract_submit", new=run_mock):
        asyncio.run(main.process_extract_queue())

    launch_mock.assert_awaited_once()
    close_mock.assert_awaited_once_with("playwright", "browser")
    assert run_mock.await_count == 2
    assert run_mock.await_args_list[0].args[1] == "browser"
    assert run_mock.await_args_list[1].args[1] == "browser"
    assert item1.status == "completed"
    assert item2.status == "completed"
    assert not main.extract_queue
    reset_extract_queue()


def test_run_extract_submit_rejects_unusable_extraction():
    note = {"success": False, "reason": "XHS_LOGIN_REQUIRED", "image_urls": []}
    with patch.dict(os.environ, {"FRONTEND_PASSWORD": "", "SERVICE_API_KEY": ""}), patch.object(main, "extract_xhs_note", new=AsyncMock(return_value=note)):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.run_extract_submit(main.XhsExtractSubmitRequest(url="http://xhslink.com/o/demo")))

    assert exc.value.status_code == 502
    assert exc.value.detail["reason"] == "XHS_LOGIN_REQUIRED"


def test_normalize_images_prefers_note_carousel_and_excludes_recommendations():
    from xhs_extractor import normalize_images

    page_data = {
        "images": [
            {"src": "https://sns-webpic-qc.xhscdn.com/a/real1!h5_1080jpg", "width": 1080, "height": 1440, "inNoteCarousel": True, "inRecommendation": False},
            {"src": "https://sns-webpic-qc.xhscdn.com/a/real2!h5_1080jpg", "width": 1080, "height": 1440, "inNoteCarousel": True, "inRecommendation": False},
            {"src": "https://sns-na-i4.xhscdn.com/recommend?imageView2/2/w/1080/format/jpg", "width": 1080, "height": 1440, "inNoteCarousel": False, "inRecommendation": True},
        ]
    }

    images = normalize_images(page_data)

    assert [img["src"] for img in images] == [
        "https://sns-webpic-qc.xhscdn.com/a/real1!h5_1080jpg",
        "https://sns-webpic-qc.xhscdn.com/a/real2!h5_1080jpg",
    ]
