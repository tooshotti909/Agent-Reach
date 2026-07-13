# -*- coding: utf-8 -*-
"""Tests for channel registry basics and health checks."""

import json
import shutil
import subprocess

from agent_reach.channels import get_all_channels, get_channel


class TestChannelRegistry:
    def test_get_channel_by_name(self):
        ch = get_channel("github")
        assert ch is not None
        assert ch.name == "github"

    def test_get_unknown_channel_returns_none(self):
        assert get_channel("not-exists") is None

    def test_all_channels_registered(self):
        channels = get_all_channels()
        names = [ch.name for ch in channels]
        assert "web" in names
        assert "github" in names
        assert "twitter" in names


class TestRedditChannel:
    def test_reports_off_when_not_installed(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _: None)
        from agent_reach.channels.reddit import RedditChannel
        status, msg = RedditChannel().check()
        assert status == "off"
        assert "rdt-cli" in msg
        assert "public-clis/rdt-cli" in msg

    def test_reports_ok_when_authenticated(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/rdt")
        fake_output = json.dumps({
            "ok": True,
            "schema_version": "1",
            "data": {"authenticated": True, "username": "testuser", "cookie_count": 1},
        })

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, fake_output, "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        from agent_reach.channels.reddit import RedditChannel
        status, msg = RedditChannel().check()
        assert status == "ok"
        assert "testuser" in msg

    def test_reports_warn_when_not_authenticated(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/rdt")
        fake_output = json.dumps({
            "ok": True,
            "schema_version": "1",
            "data": {"authenticated": False, "username": None, "cookie_count": 0},
        })

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, fake_output, "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        from agent_reach.channels.reddit import RedditChannel
        status, msg = RedditChannel().check()
        assert status == "warn"
        assert "403" in msg
        assert "rdt login" in msg
        assert "Cookie-Editor" in msg
        assert "chromewebstore.google.com" in msg

    def test_reports_warn_when_status_check_fails(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/rdt")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "not valid json{{{", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        from agent_reach.channels.reddit import RedditChannel
        status, msg = RedditChannel().check()
        assert status == "warn"

    def test_can_handle_reddit_urls(self):
        from agent_reach.channels.reddit import RedditChannel
        ch = RedditChannel()
        assert ch.can_handle("https://www.reddit.com/r/python/comments/abc123/")
        assert ch.can_handle("https://redd.it/abc123")
        assert not ch.can_handle("https://github.com/user/repo")
        assert not ch.can_handle("https://example-other.com/t/123")


