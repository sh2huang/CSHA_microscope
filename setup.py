from setuptools import find_packages, setup


setup(
    name="cshascope",
    version="0.1.0",
    author="Shuhong Huang",
    author_email="shuhong.huang@mpinb.mpg.de",
    packages=find_packages(exclude=["theknights", "theknights.*"]),
    keywords="imaging microscopy control neuroimaging",
    description="Microscope control software for the Cold Spring Harbor Asia Advanced Neuroimaging Course.",
    package_data={
        "cshascope": ["icons/*"],
        "cshascope.lightsheet": ["icons/*"],
    },
    entry_points={
        "console_scripts": [
            "cshascope=cshascope.main:main",
        ],
    },
)
