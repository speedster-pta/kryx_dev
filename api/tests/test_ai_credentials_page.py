"""ai_credentials - the consolidated superadmin-only page for every
platform-wide AI provider credential (Claude/Anthropic, Groq, ElevenLabs)
plus the per-pipeline model/effort/prompt settings for AI Replies,
Knowledge Base Ingestion and Voice Transcription (admin_pages.AICredentialsView).
Replaces six retired ModelViews (AICredentialsAdmin, AIIngestionSettingsAdmin,
GroqCredentialsAdmin, ElevenLabsCredentialsAdmin, VoiceTranscriptionSettingsAdmin,
VoiceTranscriptionConfusableSpellingAdmin).

Platform-wide, not tenant-scoped - per this project's own testing
convention for that shape of page (see TestWabaUsageView in
test_cross_org_isolation.py, TestPlatformEmailSettingsPageAccess in
test_platform_email_settings.py), the isolation property under test here
is just "a non-superadmin can't reach it".
"""


class TestAICredentialsPageAccess:
    def test_plain_staff_cannot_reach_ai_credentials(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/ai-credentials")
        assert resp.status_code == 403

    def test_org_admin_cannot_reach_ai_credentials(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/ai-credentials")
        assert resp.status_code == 403

    def test_superadmin_can_view(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.get("/ai-credentials")
        assert resp.status_code == 200
        assert "AI Credentials" in resp.text


class TestAICredentialsClaudeSave:
    def test_superadmin_can_save_and_view(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.post(
            "/ai-credentials/claude/save",
            data={"api_key": "sk-ant-topsecret"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/ai-credentials")
        assert resp.status_code == 200
        # The secret itself must never be rendered back.
        assert "sk-ant-topsecret" not in resp.text
        assert "leave blank to keep" in resp.text.lower()

    def test_keeps_existing_key_when_blank(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        client.post("/ai-credentials/claude/save", data={"api_key": "sk-ant-first"}, follow_redirects=False)
        # AI Replies form save must not wipe the key set via the Providers form.
        resp = client.post(
            "/ai-credentials/ai-replies/save",
            data={"model": "claude-sonnet-5", "effort": "high", "system_prompt": "", "custom_instructions": ""},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/ai-credentials")
        assert resp.status_code == 200
        assert "leave blank to keep" in resp.text.lower()


class TestAICredentialsAIRepliesSave:
    def test_superadmin_can_save_model_settings(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.post(
            "/ai-credentials/ai-replies/save",
            data={
                "model": "claude-opus-5",
                "effort": "max",
                "system_prompt": "You are a helpful assistant.",
                "custom_instructions": "Always be concise.",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/ai-credentials")
        assert resp.status_code == 200
        assert "You are a helpful assistant." in resp.text
        assert "Always be concise." in resp.text


class TestAICredentialsProviderSaves:
    def test_groq_save_and_mask(self, client, login_as, superadmin_username):
        # No "model" field here any more - the Groq model picker lives on
        # the Voice Transcription tab (see TestAICredentialsVoiceTranscriptionSave).
        login_as(client, superadmin_username)
        resp = client.post(
            "/ai-credentials/groq/save",
            data={"api_key": "groq-topsecret"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/ai-credentials")
        assert "groq-topsecret" not in resp.text

    def test_elevenlabs_save_and_mask(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.post(
            "/ai-credentials/elevenlabs/save",
            data={"api_key": "el-topsecret"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/ai-credentials")
        assert "el-topsecret" not in resp.text

    def test_ingestion_has_no_api_key_field(self, client, login_as, superadmin_username):
        # Ingestion no longer has its own key - it uses the one shared
        # Anthropic key configured under Providers > Claude, so saving
        # model/effort alone must succeed even with nothing else configured.
        login_as(client, superadmin_username)
        resp = client.post(
            "/ai-credentials/ingestion/save",
            data={"model": "claude-haiku-4-5", "effort": ""},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/ai-credentials?tab=ingestion")
        assert resp.status_code == 200
        # Exactly 3 api_key fields left on the whole page - Claude, Groq,
        # ElevenLabs - not a 4th one for Ingestion.
        assert resp.text.count('name="api_key"') == 3


class TestAICredentialsVoiceTranscriptionSave:
    def test_superadmin_can_save_voice_settings(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.post(
            "/ai-credentials/voice-transcription/save",
            data={
                "transcription_provider": "elevenlabs",
                "model": "claude-sonnet-5",
                "effort": "high",
                "prompt": "Clean this up.",
                "multi_language_hint": "",
                "confusable_spelling_hint": "",
                "groq_model": "",
                "elevenlabs_model": "scribe_v2",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/ai-credentials")
        assert resp.status_code == 200
        assert "Clean this up." in resp.text

    def test_switching_provider_does_not_wipe_the_other_providers_model(self, client, login_as, superadmin_username):
        # The Groq/ElevenLabs model <select>s are both always present in the
        # form regardless of which provider is active (see
        # ai_credentials.html's provider-model toggle script) - each one's
        # form value round-trips its currently-saved model when re-rendered,
        # so switching the active provider and saving must not blank out
        # the other provider's already-configured model.
        login_as(client, superadmin_username)
        client.post(
            "/ai-credentials/voice-transcription/save",
            data={
                "transcription_provider": "groq",
                "model": "claude-sonnet-5",
                "effort": "high",
                "prompt": "",
                "multi_language_hint": "",
                "confusable_spelling_hint": "",
                "groq_model": "whisper-large-v3-turbo",
                "elevenlabs_model": "",
            },
            follow_redirects=False,
        )
        resp = client.get("/ai-credentials")
        assert 'value="whisper-large-v3-turbo" selected' in resp.text

        # Switch the active provider to ElevenLabs, submitting the form as
        # a real browser would - the (now-rendered) selected groq_model
        # value round-trips back rather than an empty string.
        resp = client.post(
            "/ai-credentials/voice-transcription/save",
            data={
                "transcription_provider": "elevenlabs",
                "model": "claude-sonnet-5",
                "effort": "high",
                "prompt": "",
                "multi_language_hint": "",
                "confusable_spelling_hint": "",
                "groq_model": "whisper-large-v3-turbo",
                "elevenlabs_model": "scribe_v2_medical",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/ai-credentials")
        assert 'value="whisper-large-v3-turbo" selected' in resp.text
        assert 'value="scribe_v2_medical" selected' in resp.text


class TestAICredentialsConfusableSpellings:
    def test_superadmin_can_create_view_and_delete(self, client, login_as, superadmin_username):
        # "af" (Afrikaans/Dutch) is pre-seeded by storage.voice_transcription
        # .seed_default_confusable_spelling on every fresh DB (see that
        # function's docstring) - use an unseeded language here so this test
        # exercises its own row, not the seeded default.
        login_as(client, superadmin_username)
        resp = client.post(
            "/ai-credentials/confusable-spellings",
            data={
                "language_code": "de",
                "confusable_name": "Dutch",
                "patterns": "Some German/Dutch confusion pattern.",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        detail_url = resp.headers["location"]

        resp = client.get("/ai-credentials")
        assert detail_url in resp.text

        resp = client.get(detail_url)
        assert resp.status_code == 200
        assert "Dutch" in resp.text

        resp = client.post(f"{detail_url}/delete", follow_redirects=False)
        assert resp.status_code in (302, 303)

        resp = client.get(detail_url)
        assert resp.status_code == 404

    def test_duplicate_language_rejected(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        client.post(
            "/ai-credentials/confusable-spellings",
            data={"language_code": "nl", "confusable_name": "Afrikaans", "patterns": "some pattern"},
            follow_redirects=False,
        )
        resp = client.post(
            "/ai-credentials/confusable-spellings",
            data={"language_code": "nl", "confusable_name": "German", "patterns": "another pattern"},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_plain_staff_cannot_reach_new_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/ai-credentials/confusable-spellings/new")
        assert resp.status_code == 403


class TestModelPickersListLive:
    """The Claude/Whisper/Scribe pickers come from each provider's own
    list-models endpoint (services/model_catalog.py), not a hard-coded
    list. Provider calls are stubbed out via model_catalog._FETCHERS."""

    @staticmethod
    def _stub(monkeypatch, provider, result):
        from autosend.services import model_catalog

        calls = []

        async def fetch():
            calls.append(provider)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(model_catalog, "_cache", {})
        fetchers = dict(model_catalog._FETCHERS)
        fetchers[provider] = (fetch, fetchers[provider][1])
        monkeypatch.setattr(model_catalog, "_FETCHERS", fetchers)
        return calls

    def test_live_claude_models_rendered_with_effort_capability(self, client, login_as, superadmin_username, monkeypatch):
        from autosend.services.model_catalog import ModelChoice

        self._stub(monkeypatch, "anthropic", [
            ModelChoice("claude-future-9", "Claude Future 9"),
            ModelChoice("claude-tiny-9", "Claude Tiny 9", supports_effort=False),
        ])
        login_as(client, superadmin_username)
        resp = client.get("/ai-credentials")
        assert resp.status_code == 200
        assert 'value="claude-future-9" data-effort="yes"' in resp.text
        assert 'value="claude-tiny-9" data-effort="no"' in resp.text

    def test_provider_failure_falls_back_to_static_list(self, client, login_as, superadmin_username, monkeypatch):
        self._stub(monkeypatch, "groq", RuntimeError("network down"))
        login_as(client, superadmin_username)
        resp = client.get("/ai-credentials")
        assert resp.status_code == 200
        assert 'value="whisper-large-v3-turbo"' in resp.text

    def test_saved_model_kept_even_if_provider_no_longer_lists_it(self, client, login_as, superadmin_username, monkeypatch):
        from autosend.services.model_catalog import ModelChoice

        self._stub(monkeypatch, "anthropic", [ModelChoice("claude-future-9", "Claude Future 9")])
        login_as(client, superadmin_username)
        client.post(
            "/ai-credentials/ai-replies/save",
            data={"model": "claude-retired-1", "effort": "high", "system_prompt": "", "custom_instructions": ""},
            follow_redirects=False,
        )
        resp = client.get("/ai-credentials?tab=ai-replies")
        assert 'value="claude-retired-1" selected' in resp.text
        assert "claude-retired-1 (currently saved)" in resp.text

    def test_saving_a_key_refetches_that_providers_list(self, client, login_as, superadmin_username, monkeypatch):
        from autosend.services.model_catalog import ModelChoice

        calls = self._stub(monkeypatch, "elevenlabs", [ModelChoice("scribe_v9", "Scribe v9")])
        login_as(client, superadmin_username)
        client.get("/ai-credentials")
        client.get("/ai-credentials")
        assert calls == ["elevenlabs"]  # second load served from cache

        client.post("/ai-credentials/elevenlabs/save", data={"api_key": "el-new-key"}, follow_redirects=False)
        client.get("/ai-credentials")
        assert calls == ["elevenlabs", "elevenlabs"]

    def test_model_supports_effort_prefers_reported_capability(self, monkeypatch):
        from autosend.services import model_catalog
        from autosend.services.model_catalog import ModelChoice

        monkeypatch.setattr(model_catalog, "_cache", {
            "anthropic": (0.0, [ModelChoice("claude-sonnet-9", "Sonnet 9", supports_effort=False)]),
        })
        assert model_catalog.model_supports_effort("claude-sonnet-9") is False
        # Not in the catalogue: falls back to the "not Haiku" heuristic.
        assert model_catalog.model_supports_effort("claude-opus-9") is True
        assert model_catalog.model_supports_effort("claude-haiku-4-5") is False
        assert model_catalog.model_supports_effort(None) is False
