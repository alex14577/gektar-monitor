"""Prompter protocol and console implementation for interactive CLI.

Pure IO-boundary. No business logic here.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Prompter(Protocol):
    """Protocol for interactive user prompts."""

    def ask_text(self, prompt: str) -> str:
        """Ask for a text input.

        Args:
            prompt: The prompt string to display.

        Returns:
            User-entered text (raw, untrimmed).
        """
        ...

    def ask_yes_no(self, prompt: str) -> bool:
        """Ask a yes/no question.

        Empty string or N/n returns False. y/Y/yes returns True.

        Args:
            prompt: The prompt string to display.

        Returns:
            True for yes, False for no/empty.
        """
        ...

    def info(self, message: str) -> None:
        """Display an informational message.

        Args:
            message: Message to display.
        """
        ...

    def error(self, message: str) -> None:
        """Display an error message.

        Args:
            message: Error message to display.
        """
        ...


class ConsolePrompter(Prompter):
    """Console implementation of Prompter via input()/print()."""

    def ask_text(self, prompt: str) -> str:
        """Ask for a text input via stdin.

        Args:
            prompt: The prompt string to display.

        Returns:
            User-entered text (raw, as returned by input()).
        """
        return input(prompt)

    def ask_yes_no(self, prompt: str) -> bool:
        """Ask a yes/no question via stdin.

        Empty string or N/n = False; y/Y/yes = True.

        Args:
            prompt: The prompt string to display.

        Returns:
            True if user entered y/Y/yes, False otherwise.
        """
        answer = input(prompt).strip().lower()
        return answer in ("y", "yes")

    def info(self, message: str) -> None:
        """Print an informational message to stdout.

        Args:
            message: Message to display.
        """
        print(message)

    def error(self, message: str) -> None:
        """Print an error message to stdout.

        Args:
            message: Error message to display.
        """
        print(f"Ошибка: {message}")
