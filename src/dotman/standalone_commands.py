from __future__ import annotations

from typing import Any

from dotman.interaction_policy import InteractionRequiredError


# Repo manifests run these helpers once per render, capture, or probe, so each
# branch imports only its own command; module-level imports would add their
# startup cost to every call.
class StandaloneCommandRunner:
    """Run commands that do not need manager configuration or an engine."""

    command_names = frozenset({"rewrite", "transform", "capture", "reconcile", "render"})

    def run(self, args: Any) -> int:
        if args.command == "reconcile" and args.no_confirm:
            raise InteractionRequiredError(f"{args.command} requires interaction and is unavailable with --no-confirm")
        if args.command == "rewrite" and args.rewrite_name == "home":
            from dotman.rewrites.cli import run_home_rewrite

            return run_home_rewrite(
                action=args.rewrite_action, input_path=args.input_path, bare_tilde=args.bare_tilde
            )
        if args.command == "transform":
            from dotman.transforms.cli import run_parsed_engine

            return run_parsed_engine(args.transform_engine, args.transform_parser, args)
        if args.command == "capture" and args.capture_command == "patch":
            from dotman.template_commands import run_patch_capture

            return run_patch_capture(
                repo_path=args.repo_path,
                render_command=args.render,
                review_repo_path=args.review_repo_path,
                review_live_path=args.review_live_path,
                profile=args.profile,
                inferred_os=args.template_os,
                cpu_arch=args.template_cpu_arch,
                var_assignments=args.var,
            )
        if args.command == "reconcile" and args.reconcile_helper == "editor":
            from dotman.reconcile import run_basic_reconcile

            return run_basic_reconcile(
                repo_path=args.repo_path,
                live_path=args.live_path,
                additional_sources=args.additional_source,
                review_repo_path=args.review_repo_path,
                review_live_path=args.review_live_path,
                editor=args.editor,
            )
        if args.command == "reconcile" and args.reconcile_helper == "jinja":
            from dotman.reconcile_helpers import run_jinja_reconcile

            return run_jinja_reconcile(
                repo_path=args.repo_path,
                live_path=args.live_path,
                review_repo_path=args.review_repo_path,
                review_live_path=args.review_live_path,
                editor=args.editor,
            )
        if args.command == "render" and args.render_command == "jinja":
            from dotman.template_commands import run_jinja_render

            return run_jinja_render(
                source_path=args.source_path,
                profile=args.profile,
                inferred_os=args.template_os,
                cpu_arch=args.template_cpu_arch,
                var_assignments=args.var,
            )
        raise ValueError(f"unsupported standalone command '{args.command}'")
