---
title: "Procedural Galaxy"
<!-- date: 2026-09-01 -->
---

[Open the explorer →](/procedural-galaxy/)

An interactive 3D galaxy generator, generated entirely in the browser from a seed value.
Generated using layers of 3D perlin noise representing gas density, metallicity and various element densities (Hydrogen, Oxygen, Carbon, Helium).
The perlin maps are sampled in a grid, then the points are filtered by adjustable parameters (minimum density, hydrogen and helium thresholds), above which a system may be created.

Once the system regions have been identified the exact coordinates are randomly placed in the region of space represented by the sampled point.
Metallicity then effects the distribution of types of planets and stars, the number of celestial bodies, and their radii.

Planet formation efficiency scales with metallicity, with the snow line determining whether bodies form as terrestrials, ice giants, or gas giants. Star masses are drawn from a realistic initial mass function, producing spectral types from M dwarfs to O giants. Moons are placed within each planet's Hill sphere and outside its Roche limit. The remaining element densities are tracked per region and can be seen in the system explorer.

