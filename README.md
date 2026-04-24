# cshascope

Unified control software for the CSHA microscope.

## Install

```bash
conda env create -f environment.yml
conda activate cshascope
pip install -e .
```

## Run

Light-sheet microscope with NI hardware:

```bash
cshascope --lightsheet
```

Light-sheet microscope with simulated hardware:

```bash
cshascope --lightsheet --scopeless
```

Point-scanning microscope:

```bash
cshascope --pointscan
```
