import click

@click.command()
@click.option("--scopeless", is_flag=True, help="Scopeless mode for simulated hardware")
def main(scopeless):
    from cshascope.lightsheet import run

    return run(scopeless=scopeless)
