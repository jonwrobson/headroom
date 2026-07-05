"""Tests for auth token health checking functionality."""

import httpx
import pytest

from headroom.proxy.auth_token_pool import TokenInfo, load_tokens_from_file


class TestTokenInfoParsing:
    """Test parsing of token file formats."""

    def test_csv_format(self, tmp_path):
        """CSV format: ID,TOKEN"""
        token_file = tmp_path / "tokens.txt"
        token_file.write_text(
            "prod-token,sk-ant-api03-prod\n"
            "backup-token,sk-ant-api03-backup\n"
            "dev-token,sk-ant-api03-dev\n"
        )

        tokens = load_tokens_from_file(token_file)

        assert len(tokens) == 3
        assert tokens[0].id == "prod-token"
        assert tokens[0].token == "sk-ant-api03-prod"
        assert tokens[1].id == "backup-token"
        assert tokens[1].token == "sk-ant-api03-backup"
        assert tokens[2].id == "dev-token"
        assert tokens[2].token == "sk-ant-api03-dev"

    def test_plain_format(self, tmp_path):
        """Plain format: TOKEN (auto-generates ID from suffix)"""
        token_file = tmp_path / "tokens.txt"
        token_file.write_text(
            "sk-ant-api03-prod\n" "sk-ant-api03-backup\n" "sk-ant-api03-dev\n"
        )

        tokens = load_tokens_from_file(token_file)

        assert len(tokens) == 3
        assert tokens[0].id == "...prod"
        assert tokens[0].token == "sk-ant-api03-prod"
        assert tokens[1].id == "...ckup"
        assert tokens[1].token == "sk-ant-api03-backup"
        assert tokens[2].id == "...-dev"
        assert tokens[2].token == "sk-ant-api03-dev"

    def test_mixed_format(self, tmp_path):
        """Mixed CSV and plain format"""
        token_file = tmp_path / "tokens.txt"
        token_file.write_text(
            "prod-token,sk-ant-api03-prod\n"
            "sk-ant-api03-backup\n"
            "dev-token,sk-ant-api03-dev\n"
        )

        tokens = load_tokens_from_file(token_file)

        assert len(tokens) == 3
        assert tokens[0].id == "prod-token"
        assert tokens[1].id == "...ckup"
        assert tokens[2].id == "dev-token"

    def test_ignores_comments_and_blanks(self, tmp_path):
        """Comments and blank lines are ignored"""
        token_file = tmp_path / "tokens.txt"
        token_file.write_text(
            "# This is a comment\n"
            "\n"
            "prod-token,sk-ant-api03-prod\n"
            "  \n"
            "# Another comment\n"
            "backup-token,sk-ant-api03-backup\n"
        )

        tokens = load_tokens_from_file(token_file)

        assert len(tokens) == 2
        assert tokens[0].id == "prod-token"
        assert tokens[1].id == "backup-token"

    def test_deduplicates_tokens(self, tmp_path):
        """Duplicate tokens are dropped (first wins)"""
        token_file = tmp_path / "tokens.txt"
        token_file.write_text(
            "first,sk-ant-api03-duplicate\n"
            "second,sk-ant-api03-duplicate\n"
            "third,sk-ant-api03-unique\n"
        )

        tokens = load_tokens_from_file(token_file)

        assert len(tokens) == 2
        assert tokens[0].id == "first"  # First occurrence wins
        assert tokens[0].token == "sk-ant-api03-duplicate"
        assert tokens[1].id == "third"
        assert tokens[1].token == "sk-ant-api03-unique"

    def test_handles_whitespace(self, tmp_path):
        """Whitespace is trimmed"""
        token_file = tmp_path / "tokens.txt"
        token_file.write_text(
            "  prod-token  ,  sk-ant-api03-prod  \n"
            "   sk-ant-api03-backup   \n"
        )

        tokens = load_tokens_from_file(token_file)

        assert len(tokens) == 2
        assert tokens[0].id == "prod-token"
        assert tokens[0].token == "sk-ant-api03-prod"
        assert tokens[1].id == "...ckup"
        assert tokens[1].token == "sk-ant-api03-backup"


class TestTokenPoolWithTokenInfo:
    """Test TokenPool with TokenInfo objects."""

    def test_accepts_token_info_list(self):
        """TokenPool accepts list of TokenInfo"""
        from headroom.proxy.auth_token_pool import TokenPool

        tokens = [
            TokenInfo(id="prod", token="sk-ant-prod"),
            TokenInfo(id="backup", token="sk-ant-backup"),
        ]

        pool = TokenPool(tokens)

        assert len(pool) == 2
        assert pool.current() == "sk-ant-prod"

    def test_backward_compatible_with_strings(self):
        """TokenPool still accepts plain string list"""
        from headroom.proxy.auth_token_pool import TokenPool

        tokens = ["sk-ant-prod", "sk-ant-backup"]

        pool = TokenPool(tokens)

        assert len(pool) == 2
        assert pool.current() == "sk-ant-prod"

    def test_from_file_with_csv_format(self, tmp_path):
        """TokenPool.from_file works with CSV format"""
        from headroom.proxy.auth_token_pool import TokenPool

        token_file = tmp_path / "tokens.txt"
        token_file.write_text("prod,sk-ant-prod\nbackup,sk-ant-backup\n")

        pool = TokenPool.from_file(token_file)

        assert len(pool) == 2
        assert pool.current() == "sk-ant-prod"


@pytest.mark.asyncio
class TestTokenHealthCheck:
    """Test token health checking functionality."""

    async def test_check_token_health_active(self, respx_mock):
        """Active token returns healthy status"""
        from headroom.proxy.auth_token_pool import TokenInfo, TokenPool

        # Mock successful API response
        respx_mock.post("https://api.example.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json={"id": "msg_123", "content": [{"type": "text", "text": "ok"}]}
            )
        )

        token_info = TokenInfo(id="test-token", token="sk-ant-test")
        pool = TokenPool([token_info])

        result = await pool.check_token_health(token_info, "https://api.example.com")

        assert result["id"] == "test-token"
        assert result["status"] == "active"
        assert result["healthy"] is True

    async def test_check_token_health_budget_exceeded(self, respx_mock):
        """Budget exceeded token returns unhealthy status"""
        from headroom.proxy.auth_token_pool import TokenInfo, TokenPool

        # Mock budget exceeded error
        respx_mock.post("https://api.example.com/v1/messages").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "type": "budget_exceeded",
                        "message": "Budget has been exceeded! Current: 3212.27, Max: 3200.0",
                    }
                },
            )
        )

        token_info = TokenInfo(id="test-token", token="sk-ant-test")
        pool = TokenPool([token_info])

        result = await pool.check_token_health(token_info, "https://api.example.com")

        assert result["id"] == "test-token"
        assert result["status"] == "budget_exceeded"
        assert result["healthy"] is False
        assert "Budget has been exceeded" in result["message"]

    async def test_check_token_health_unauthorized(self, respx_mock):
        """Unauthorized token returns unhealthy status"""
        from headroom.proxy.auth_token_pool import TokenInfo, TokenPool

        # Mock 401 unauthorized
        respx_mock.post("https://api.example.com/v1/messages").mock(
            return_value=httpx.Response(
                401, json={"error": {"type": "auth_error", "message": "Invalid token"}}
            )
        )

        token_info = TokenInfo(id="test-token", token="sk-ant-test")
        pool = TokenPool([token_info])

        result = await pool.check_token_health(token_info, "https://api.example.com")

        assert result["id"] == "test-token"
        assert result["status"] == "unauthorized"
        assert result["healthy"] is False

    async def test_check_all_tokens_parallel(self, respx_mock):
        """check_all_tokens tests all tokens in parallel"""
        from headroom.proxy.auth_token_pool import TokenInfo, TokenPool

        # Mock responses for different tokens
        respx_mock.post("https://api.example.com/v1/messages").mock(
            side_effect=[
                httpx.Response(
                    200, json={"id": "msg_1", "content": [{"type": "text", "text": "ok"}]}
                ),  # active
                httpx.Response(
                    400,
                    json={"error": {"type": "budget_exceeded", "message": "Budget exceeded"}},
                ),  # budget
                httpx.Response(
                    200, json={"id": "msg_3", "content": [{"type": "text", "text": "ok"}]}
                ),  # active
            ]
        )

        tokens = [
            TokenInfo(id="token-1", token="sk-ant-1"),
            TokenInfo(id="token-2", token="sk-ant-2"),
            TokenInfo(id="token-3", token="sk-ant-3"),
        ]
        pool = TokenPool(tokens)

        results = await pool.check_all_tokens("https://api.example.com")

        assert len(results) == 3
        assert results[0]["status"] == "active"
        assert results[1]["status"] == "budget_exceeded"
        assert results[2]["status"] == "active"

# Made with Bob
