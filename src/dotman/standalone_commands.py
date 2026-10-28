from __future__ import annotations

from typing import Any


# Repo manifests run these helpers once per render, capture, or probe, so each
# branch imports only its own command; module-level imports would add their
# startup cost to every call.
class StandaloneCommandRunner:
    """Run commands that do not need manager configuration or an engine."""

    command_names = frozenset({"rewrite", "transform", "capture", "render"})

    def run(self, args: Any) -> int:
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
