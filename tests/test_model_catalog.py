"""Source/model separation and read compatibility; no live model calls."""
import copy
import json

import pytest

from station import db, modelcfg, modelcatalog as catalog


@pytest.fixture
def user():
    return db.create_user("catalog-owner", "pw")["id"]


def config():
    cfg = catalog.upgrade(modelcfg.default_config())
    for p in cfg["providers"]:
        p["api_key"] = "synthetic-key-"+p["id"]
    return cfg


def test_view_migrates_models_and_assignments_without_db_write(user):
    original = modelcfg.default_config()
    modelcfg.save_config(user, original)
    before = db.model_config_load(user)
    view = catalog.upgrade(modelcfg.load_config(user))
    assert db.model_config_load(user) == before
    agnes = next(p for p in view["providers"] if p["id"] == "agnes")
    assert len(agnes["models"]) == 1 and agnes["models"][0]["capabilities"] == ["video"]
    qwen = next(p for p in view["providers"] if p["id"] == "qwen")
    assert len(qwen["models"]) == 2
    assert set(qwen["models"][0]["capabilities"]) == {"chat", "vision"}
    assert view["slots"]["route"] == ["qwen~legacy-route"]
    assert catalog.upgrade(view) == view


def test_two_models_from_same_source_resolve_in_order_and_share_key(user):
    cfg = config()
    qwen = next(p for p in cfg["providers"] if p["id"] == "qwen")
    qwen["models"].append({"id":"alternate", "label":"备用对话", "name":"alternative-model",
                           "capabilities":["chat"], "reasoning_effort":"high"})
    cfg["slots"]["chat"] = ["qwen~alternate", "qwen~legacy-chat", "glm~legacy-chat"]
    modelcfg.save_config(user, cfg)
    entries = modelcfg.resolve(user, "chat")
    assert [e["model"] for e in entries] == ["alternative-model", "qwen-vl-max", "glm-5.3-flash"]
    assert entries[0]["api_key"] == entries[1]["api_key"] == qwen["api_key"]
    assert entries[0]["reasoning_effort"] == "high" and entries[1]["reasoning_effort"] == ""
    assert entries[0]["provider_id"] == "qwen" and entries[0]["model_id"] == "alternate"
    assert db.model_config_load(user)["version"] == 2
    assert "synthetic-key" not in json.dumps(modelcfg.mask_config(modelcfg.load_config(user)))


def test_same_model_can_be_assigned_to_multiple_capabilities(user):
    modelcfg.save_config(user, config())
    a = modelcfg.resolve_one(user,"glm~legacy-chat","chat")[0]
    b = modelcfg.resolve_one(user,"glm~legacy-chat","vision")[0]
    assert a["model"] == b["model"]
    assert a["reasoning_effort"] == "low" and b["reasoning_effort"] == ""


def test_profile_protocol_is_independent_of_source(user):
    cfg = config()
    p = {"id":"zen","label":"Zen","base_url":"https://opencode.ai/zen/v1","api_key":"synthetic-zen-key",
         "models":[{"id":"chat","name":"ordinary-chat","capabilities":["chat"]},
                   {"id":"jev","name":"jev-1.13-free","capabilities":["route"],"route_protocol":"systemone","route_threshold":0.85}]}
    cfg["providers"].append(p)
    cfg["slots"]["chat"] = ["zen~chat"]
    cfg["slots"]["route"] = ["zen~jev"]
    modelcfg.save_config(user,cfg)
    assert modelcfg.resolve(user,"chat")[0]["route_protocol"] == "chat_completions"
    assert modelcfg.resolve(user,"route")[0]["route_protocol"] == "systemone"
    assert modelcfg.resolve(user,"route")[0]["route_threshold"] == .85
    assert modelcfg.probe_entry(user,"zen","route","jev")[2] == "jev-1.13-free"


@pytest.mark.parametrize("change", [
    {"capabilities":[]},{"capabilities":["unknown"]},{"id":"bad~id"},{"name":""},
    {"capabilities":["chat","route"],"route_protocol":"systemone"},
    {"capabilities":["video"],"reasoning_effort":"high"}, {"route_threshold":float("nan")}])
def test_invalid_profile_is_rejected(user,change):
    cfg=config();p=cfg["providers"][0]
    p["models"][0].update(change)
    with pytest.raises(ValueError):modelcfg.save_config(user,cfg)


def test_wrong_capability_and_unknown_model_are_rejected(user):
    cfg=config()
    for ref in ["agnes~legacy-video","qwen~missing"]:
        cfg["slots"]["chat"]=[ref]
        with pytest.raises(ValueError,match="不存在|能力"):modelcfg.save_config(user,cfg)


def test_model_deletion_removes_bindings_without_resurrecting_defaults(user):
    cfg=config();modelcfg.save_config(user,cfg)
    cfg= modelcfg.load_config(user)
    next(p for p in cfg["providers"] if p["id"]=="agnes")["models"]=[]
    modelcfg.save_config(user,cfg)
    assert modelcfg.load_config(user)["slots"]["video"] == []
    assert modelcfg.resolve(user,"video") == []


def test_old_client_does_not_erase_new_model_profiles(user):
    cfg=config();modelcfg.save_config(user,cfg)
    old_client=copy.deepcopy(cfg)
    for p in old_client["providers"]:p.pop("models")
    old_client["providers"][0]["api_key"]="rotated-key"
    modelcfg.save_config(user,old_client)
    assert modelcfg.resolve(user,"chat")[0]["api_key"] == "rotated-key"
    assert "models" in modelcfg.load_config(user)["providers"][0]
    old_client["providers"][0]["text_model"]="changed-via-old-ui"
    with pytest.raises(ValueError,match="刷新"):modelcfg.save_config(user,old_client)


def test_vision_menu_lists_profiles_and_preserves_legacy_cache(user):
    cfg=config()
    p=next(p for p in cfg["providers"] if p["id"]=="qwen")
    p["models"].append({"id":"vision2","name":"new-vision","capabilities":["vision"]})
    modelcfg.save_config(user,cfg)
    choices=modelcfg.vision_providers(user)
    assert {"qwen~legacy-chat","qwen~vision2"} <= {c["id"] for c in choices}
    legacy=modelcfg.resolve(user,"vision")
    assert modelcfg.chain_label(legacy)=="qwen"
    changed=modelcfg.resolve_one(user,"qwen~vision2","vision")
    before=modelcfg.chain_label(changed)
    assert "~" in before and ":" not in before
    p["models"][-1]["name"]="renamed-vision"
    modelcfg.save_config(user,cfg)
    assert modelcfg.chain_label(modelcfg.resolve_one(user,"qwen~vision2","vision")) != before


def test_per_model_probe_does_not_silently_test_another_capability(user):
    modelcfg.save_config(user,config())
    with pytest.raises(ValueError,match="能力"):
        modelcfg.probe_entry(user,"agnes","chat","legacy-video")


def test_search_source_and_key_survive_profile_conversion(user):
    cfg=config();modelcfg.save_config(user,cfg)
    entry,_=modelcfg.search_entry(user)
    assert entry["id"]=="deepseek" and entry["api_key"]=="synthetic-key-deepseek"


@pytest.mark.parametrize("ref", ["qwen~legacy-chat", "qwen~vision2"])
def test_archive_model_menu_and_reuse_match_resolved_cache_channel(user,tmp_path,monkeypatch,ref):
    from station.skills.registry import get_registry
    from station.core.context import Context
    from archive.service import interactive as it
    cfg=config()
    p=next(p for p in cfg["providers"] if p["id"]=="qwen")
    p["models"].append({"id":"vision2","name":"other-vision","capabilities":["vision"]})
    modelcfg.save_config(user,cfg)
    channel=modelcfg.chain_label(modelcfg.resolve_one(user,ref,"vision"))
    project=str(tmp_path/'volume'/'project.json')
    tmp_path.joinpath('current.json').write_text(json.dumps({"project":project,"person":"test"}),encoding='utf-8')
    it.register_volume(project,'test',user)
    it.save_state(project,[{"uid":"m1"}],[],"test",user_id=user,channel=channel)
    it.set_active(project,channel)
    monkeypatch.setattr(it,'cache_coverage',lambda pj,ch:(1,1) if ch==channel else (0,1))
    ctx=Context(skill_id='archive',data_dir=tmp_path,user_id=user)
    tools={t.leaf():t for t in get_registry().get('archive').tools}
    menu=tools['list_vision_models'].run(ctx)
    line=next(line for line in menu.splitlines() if f'（{ref}）' in line)
    assert '已识别过' in line and '当前展示' in line
    # No job/model call: a saved result must switch directly to that channel.
    result=tools['recognize'].run(ctx,provider=ref)
    assert '已经读过' in result['text']
    result=tools['use_model'].run(ctx,provider=ref)
    assert '已切到' in result['text'] and it.active_channel(project)==channel


def test_api_profile_test_vision_sends_actual_image(monkeypatch,user):
    from station.app import server
    modelcfg.save_config(user,config())
    monkeypatch.setattr(server,"require_user",lambda request:{"id":user})
    monkeypatch.setattr(server,"_test_allowed",lambda uid:True)
    received=[]
    class Model:
        def __init__(self,**kwargs):received.append(kwargs)
        def invoke(self,messages):received.append(messages)
    monkeypatch.setattr('station.core.model.import_chat_openai',lambda:Model)
    out=server.test_model_config(server.ProviderTestIn(provider_id='qwen',model_id='legacy-chat',slot='vision'),None)
    assert out['ok']
    assert received[0]['model']=='qwen-vl-max'
    assert received[1][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    video=server.test_model_config(server.ProviderTestIn(provider_id='agnes',model_id='legacy-video',slot='video'),None)
    assert video['skip'] and video['ok'] is None and len(received)==2
