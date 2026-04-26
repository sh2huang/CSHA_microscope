import click


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--lightsheet",
    is_flag=True,
    help="Run the light-sheet microscope interface.",
)
@click.option(
    "--pointscan",
    is_flag=True,
    help="Run the point-scanning microscope interface.",
)
@click.option(
    "--scopeless",
    is_flag=True,
    help="Use simulated light-sheet hardware.",
)
def main(lightsheet, pointscan, scopeless):
    selected_modes = int(lightsheet) + int(pointscan)

    if selected_modes != 1:
        raise click.UsageError("Choose exactly one mode: --lightsheet or --pointscan.")

    if scopeless and not lightsheet:
        raise click.UsageError("--scopeless is only valid with --lightsheet.")

    if lightsheet:
        from cshascope.runners.lightsheet import run

        return run(scopeless=scopeless)

    from cshascope.runners.pointscan import run

    return run()


if __name__ == "__main__":
    main()
