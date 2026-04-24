from setuptools import find_packages, setup


setup(
    name="cshascope",
    version="0.1.0",
    packages=find_packages(),
    package_data={
        "sashimi": ["icons/*"],
    },
    entry_points={
        "console_scripts": [
            "cshascope=cshascope.main:main",
        ],
    },
)
