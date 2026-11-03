"""Unit tests for the pure helpers of the Copilot auto proxy.

The network paths (session opening, intent routing, completions) are exercised
live against the Copilot API, not here. What is tested here is the pure logic
that decides *what* to send — the prompt extraction, the conversation key that
makes routing sticky, and the configuration contract.
"""

from __future__ import annotations

import json

import pytest

from app import ConfigError, CopilotClient, load_config


class _NullSession:
    """Stands in for aiohttp's ClientSession — the helpers never use it."""


def make_client(**overrides) -> CopilotClient:
    config = {
        "github_token": "test-token",
        "shared_secret": "test-secret",
        **overrides,
    }
    return CopilotClient(config, _NullSession())


def user_message(text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def assistant_message(text: str) -> dict:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


class TestTextExtraction:
    def test_extracts_plain_string_content(self):
        items = [{"type": "message", "role": "user", "content": "hello"}]
        assert CopilotClient._text_from_items(items) == ["hello"]

    def test_extracts_text_parts(self):
        items = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "one"},
                    {"type": "input_image", "image_url": "http://x/y.png"},
                    {"type": "input_text", "text": "two"},
                ],
            }
        ]
        assert CopilotClient._text_from_items(items) == ["one", "two"]

    def test_ignores_non_message_items(self):
        items = [
            {"type": "function_call", "name": "x", "arguments": "{}"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "keep"}]},
        ]
        assert CopilotClient._text_from_items(items) == ["keep"]

    def test_non_list_input_is_empty(self):
        assert CopilotClient._text_from_items(None) == []
        assert CopilotClient._text_from_items("nope") == []


class TestConversationKey:
    def test_key_is_stable_across_turns_of_the_same_thread(self):
        client = make_client()
        first = {"input": [user_message("Compare Postgres and MySQL")]}
        later = {
            "input": [
                user_message("Compare Postgres and MySQL"),
                assistant_message("..."),
                user_message("And SQLite?"),
            ]
        }
        assert client._conversation_key(first) == client._conversation_key(later)

    def test_different_threads_get_different_keys(self):
        client = make_client()
        a = {"input": [user_message("thread one")]}
        b = {"input": [user_message("thread two")]}
        assert client._conversation_key(a) != client._conversation_key(b)

    def test_key_falls_back_to_instructions_without_user_input(self):
        client = make_client()
        payload = {"input": [], "instructions": "system prompt"}
        assert client._conversation_key(payload) == client._conversation_key(payload)
        assert client._conversation_key(payload) != client._conversation_key({"input": [], "instructions": "other"})

    def test_key_is_a_hex_digest(self):
        client = make_client()
        key = client._conversation_key({"input": [user_message("x")]})
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)


class TestCurrentPrompt:
    def test_takes_the_last_user_turn(self):
        client = make_client()
        payload = {
            "input": [
                user_message("first"),
                assistant_message("answer"),
                user_message("second"),
            ]
        }
        assert client._current_prompt(payload) == "second"

    def test_is_empty_without_user_turns(self):
        client = make_client()
        assert client._current_prompt({"input": [assistant_message("answer")]}) == ""

    def test_is_truncated(self):
        client = make_client()
        payload = {"input": [user_message("x" * 9000)]}
        assert len(client._current_prompt(payload)) == 8000


class TestLoadConfig:
    def test_rejects_a_missing_file(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(str(tmp_path / "absent.json"))

    def test_rejects_invalid_json(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(str(path))

    def test_rejects_a_config_without_a_credential(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"shared_secret": "s"}), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(str(path))

    def test_rejects_a_config_without_a_shared_secret(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"github_token": "t"}), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(str(path))

    def test_accepts_a_complete_config(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"github_token": "t", "shared_secret": "s"}), encoding="utf-8"
        )
        config = load_config(str(path))
        assert config["github_token"] == "t"


class TestClientDefaults:
    def test_default_api_bases_and_hints(self):
        client = make_client()
        assert client.api_bases[0] == "https://api.individual.githubcopilot.com"
        assert "https://api.githubcopilot.com" in client.api_bases
        assert client.model_hints == ["auto"]

    def test_api_bases_are_normalised(self):
        client = make_client(api_bases=["https://example.test/v1/"])
        assert client.api_bases == ["https://example.test/v1"]

    def test_bearer_header_uses_the_configured_credential(self):
        client = make_client()
        headers = client.base_headers()
        assert headers["Authorization"] == "Bearer test-token"
        assert headers["X-GitHub-Api-Version"] == "2025-07-16"

    def test_exchange_can_be_disabled(self):
        client = make_client(allow_exchange=False)
        assert client.allow_exchange is False
