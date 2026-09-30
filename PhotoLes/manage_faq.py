"""Command-line backend for managing customer questions and answers."""

from __future__ import annotations

import argparse
import sys

from faq_repository import FAQRepository
from settings import database_path, load_local_env


def prompt_if_missing(value: str | None, label: str) -> str:
    if value is not None:
        return value
    return input(f"{label}: ").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the questions shown by the photobooth Telegram bot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the FAQ database.")

    add_parser = subparsers.add_parser("add", help="Add a question and answer.")
    add_parser.add_argument("-q", "--question")
    add_parser.add_argument("-a", "--answer")
    add_parser.add_argument("-c", "--category", default="General")
    add_parser.add_argument("-o", "--order", type=int, default=0)

    list_parser = subparsers.add_parser("list", help="Show saved Q&As and their IDs.")
    list_parser.add_argument("--all", action="store_true", help="Include disabled Q&As.")

    update_parser = subparsers.add_parser("update", help="Change an existing Q&A.")
    update_parser.add_argument("id", type=int)
    update_parser.add_argument("-q", "--question")
    update_parser.add_argument("-a", "--answer")
    update_parser.add_argument("-c", "--category")
    update_parser.add_argument("-o", "--order", type=int)

    for command, help_text in (
        ("enable", "Show a disabled Q&A in the bot."),
        ("disable", "Hide a Q&A without deleting it."),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("id", type=int)

    delete_parser = subparsers.add_parser("delete", help="Permanently delete a Q&A.")
    delete_parser.add_argument("id", type=int)
    delete_parser.add_argument("--yes", action="store_true", help="Skip confirmation.")
    return parser


def print_faqs(repository: FAQRepository, include_inactive: bool) -> None:
    faqs = repository.list_all(include_inactive=include_inactive)
    if not faqs:
        print("No Q&As saved yet. Add one with: python manage_faq.py add")
        return

    for faq in faqs:
        status = "active" if faq.is_active else "disabled"
        print(f"[{faq.id}] {faq.question}")
        print(f"    Answer: {faq.answer}")
        print(f"    Category: {faq.category} | Order: {faq.sort_order} | {status}\n")


def main() -> int:
    load_local_env()
    args = build_parser().parse_args()
    repository = FAQRepository(database_path())
    repository.initialize()

    try:
        if args.command == "init":
            print(f"FAQ database is ready at {repository.database_path}")
        elif args.command == "add":
            faq_id = repository.add(
                prompt_if_missing(args.question, "Question"),
                prompt_if_missing(args.answer, "Answer"),
                args.category,
                args.order,
            )
            print(f"Added Q&A #{faq_id}.")
        elif args.command == "list":
            print_faqs(repository, args.all)
        elif args.command == "update":
            if all(
                value is None
                for value in (args.question, args.answer, args.category, args.order)
            ):
                raise ValueError("Provide at least one field to update (-q, -a, -c, or -o).")
            if not repository.update(
                args.id,
                question=args.question,
                answer=args.answer,
                category=args.category,
                sort_order=args.order,
            ):
                raise ValueError(f"Q&A #{args.id} does not exist.")
            print(f"Updated Q&A #{args.id}.")
        elif args.command in {"enable", "disable"}:
            active = args.command == "enable"
            if not repository.set_active(args.id, active):
                raise ValueError(f"Q&A #{args.id} does not exist.")
            print(f"{'Enabled' if active else 'Disabled'} Q&A #{args.id}.")
        elif args.command == "delete":
            if not args.yes:
                confirmation = input(f"Permanently delete Q&A #{args.id}? Type yes: ")
                if confirmation.strip().lower() != "yes":
                    print("Delete cancelled.")
                    return 0
            if not repository.delete(args.id):
                raise ValueError(f"Q&A #{args.id} does not exist.")
            print(f"Deleted Q&A #{args.id}.")
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
