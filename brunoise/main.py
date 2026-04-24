import click

@click.command()
def main():
    from cshascope.pointscan import run

    return run()
