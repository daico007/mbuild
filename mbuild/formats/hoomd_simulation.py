import warnings 
import itertools
import numpy as np

import parmed as pmd
import mbuild as mb
from mbuild.utils.sorting import natural_sort
from mbuild.utils.io import import_
from mbuild.utils.conversion import RB_to_OPLS

from .hoomd_snapshot import to_hoomdsnapshot

hoomd = import_("hoomd")
hoomd.md = import_("hoomd.md")
hoomd.md.pair = import_("hoomd.md.pair")
hoomd.md.special_pair = import_("hoomd.md.special_pair")
hoomd.md.charge = import_("hoomd.md.charge")
hoomd.md.bond = import_("hoomd.md.bond")
hoomd.md.angle = import_("hoomd.md.angle")
hoomd.md.dihedral = import_("hoomd.md.dihedral")
hoomd.group = import_("hoomd.group")


def create_hoomd_simulation(structure, ref_distance=1.0, ref_mass=1.0,
              ref_energy=1.0, mixing_rule='lorentz', r_cut=1.2, 
              snapshot_kwargs={}, 
              pppm_kwargs={'Nx':1, 'Ny':1, 'Nz':1, 'order':4}):
    """ Convert a parametrized pmd.Structure to hoomd.SimulationContext

    Parameters
    ----------
    structure : parmed.Structure
        ParmEd Structure object
    ref_distance : float, optional, default=1.0
        Reference distance for conversion to reduced units
    ref_mass : float, optional, default=1.0
        Reference mass for conversion to reduced units
    ref_energy : float, optional, default=1.0
        Reference energy for conversion to reduced units
    mixing_rule : str, optional, default 'lorentz'
        Specify a mixing rule to identify LJ cross-interactions
    r_cut : float, optional, default 1.2
        Cutoff radius, in reduced units
    snapshot_kwargs : dict
        Kwargs to pass to to_hoomdsnapshot
    pppm_kwargs : dict
        Kwargs to pass to hoomd's pppm function

    Notes
    -----
    While nothing is returned, the hoomd.SimulationContext is accessible via
    `hoomd.context.current`.
    If you pass a non-parametrized pmd.Structure, you will not have
    angle, dihedral, or force field information. You may be better off
    creating a hoomd.Snapshot"""

    if isinstance(structure, mb.Compound):
        raise ValueError("You passed mb.Compound to create_hoomd_simulation, " +
                "there will be no angles, dihedrals, or force field parameters. " +
                "Please use " + 
                "hoomd_snapshot.to_hoomdsnapshot to create a hoomd.Snapshot, " +
                "then create your own hoomd context " +
                "and pass your hoomd.Snapshot " +
                "to hoomd.init.read_snapshot()")
    elif not isinstance(structure, pmd.Structure):
        raise ValueError("Please pass a parmed.Structure to " + 
                    "create_hoomd_simulation")
    hoomd.context.initialize("")

    snapshot = to_hoomdsnapshot(structure, ref_distance=ref_distance,
            ref_mass=ref_mass, ref_energy=ref_energy, **snapshot_kwargs)
    hoomd.init.read_snapshot(snapshot)

    nl = hoomd.md.nlist.cell()
    nl.reset_exclusions(exclusions=['1-2', '1-3'])

    if structure.atoms[0].type != '':
        print("Processing LJ and QQ")
        lj = _init_hoomd_lj(structure, nl, r_cut=r_cut, mixing_rule='lorentz',
                ref_distance=ref_distance, ref_energy=ref_energy)
        qq = _init_hoomd_qq(structure, nl, r_cut=r_cut, **pppm_kwargs)
    if structure.adjusts:
        print("Processing 1-4 interactions, adjusting neighborlist exclusions")
        lj_14, qq_14 =  _init_hoomd_14_pairs(structure, nl,
                ref_distance=ref_distance, ref_energy=ref_energy)
    if structure.bond_types:
        print("Processing harmonic bonds")
        harmonic_bond = _init_hoomd_bonds(structure,
                ref_distance=ref_distance, ref_energy=ref_energy)
    if structure.angle_types:
        print("Processing harmonic angles")
        harmonic_angle = _init_hoomd_angles(structure,
                ref_energy=ref_energy)
    if structure.dihedral_types:
        print("Processing periodic torsions")
        periodic_torsions = _init_hoomd_dihedrals(structure,
                ref_energy=ref_energy)
    if structure.rb_torsion_types:
        print("Processing RB torsions")
        rb_torsions = _init_hoomd_rb_torsions(structure,
                ref_energy=ref_energy)
    print("HOOMD SimulationContext updated from ParmEd Structure")

def _init_hoomd_lj(structure, nl, r_cut=1.2, mixing_rule='lorentz',
        ref_distance=1.0, ref_energy=1.0):
    """ LJ parameters """
    # Identify the unique atom types before setting
    atom_type_params = {}
    for atom in structure.atoms:
        if atom.type not in atom_type_params:
            atom_type_params[atom.type] = atom.atom_type

    # Set the hoomd parameters for self-interactions
    lj = hoomd.md.pair.lj(r_cut, nl)
    for name, atom_type in atom_type_params.items():
        lj.pair_coeff.set(name, name, 
                sigma=atom_type.sigma/ref_distance,
                epsilon=atom_type.epsilon/ref_energy)

    # Cross interactions, mixing rules, NBfixes
    all_atomtypes = sorted(atom_type_params.keys())
    for a1, a2 in itertools.combinations_with_replacement(all_atomtypes, 2):
        nb_fix_info = atom_type_params[a1].nbfix.get(a2, None)
        # nb_fix_info = (rmin, eps, rmin14, eps14)
        if nb_fix_info is None:
            # No nbfix means use mixing rule to find cross-interaction
            if mixing_rule.lower() == 'lorentz':
                sigma = ((atom_type_params[a1].sigma + atom_type_params[a2].sigma) 
                        / (2 * ref_distance))
                epsilon = ((atom_type_params[a1].epsilon * 
                        atom_type_params[a2].epsilon) / 
                        ref_energy**2)
            else:
                raise ValueError("Mixing rule {} ".format(mixing_rule) + 
                                "not supported, use lorentz")
        else:
            # If we have nbfix info, use it
            sigma = nb_fix_info[0] / (ref_distance*(2 ** (1/6)))
            epsilon = nb_fix_info[1] / ref_energy
        lj.pair_coeff.set(a1, a2, sigma=sigma, epsilon=epsilon)

    return lj

def _init_hoomd_qq(structure, nl, Nx=1, Ny=1, Nz=1, order=4, r_cut=1.2):
    """ Charge interactions """
    charged = hoomd.group.charged()
    qq = hoomd.md.charge.pppm(charged, nl)
    qq.set_params(Nx, Ny, Nz, order, r_cut)
    return qq


def _init_hoomd_14_pairs(structure, nl, r_cut=1.2, ref_distance=1.0, ref_energy=1.0):
    """Special_pairs to handle 14 scalings
    
    See discussion: https://groups.google.com/forum/
    #!topic/hoomd-users/iZ9WCpHczg0 """

    # Update neighborlist to exclude 1-4 interactions, 
    # but impose a special_pair force to handle these pairs
    nl.reset_exclusions(exclusions=['1-2', '1-3', '1-4']) 

    lj_14 = hoomd.md.special_pair.lj()
    qq_14 = hoomd.md.special_pair.coulomb()
    params_14 = {}
    # Identify unique 14 scalings
    for adjust in structure.adjusts:
        t1 = adjust.atom1.type
        t2 = adjust.atom2.type
        ps = '-'.join(sorted([t1, t2]))
        if ps not in params_14:
            params_14[ps] = adjust.type
    for name, adjust_type in params_14.items():
        lj_14.pair_coeff.set(name, 
                sigma=adjust_type.sigma/ref_distance,
                # The adjust epsilon alreayd carries the scaling
                epsilon=adjust_type.epsilon/ref_energy, 
                # Do NOT use hoomd's alpha to modify any LJ terms
                alpha=1,
                r_cut=r_cut)
        qq_14.pair_coeff.set(name,
                alpha=adjust_type.chgscale,
                r_cut=r_cut)

    return lj_14, qq_14

def _init_hoomd_bonds(structure, ref_distance=1.0, ref_energy=1.0):
    """ Harmonic bonds """
    # Identify the unique bond types before setting
    bond_type_params = {}
    for bond in structure.bonds:
        t1, t2 = bond.atom1.type, bond.atom2.type
        t1, t2 = sorted([t1, t2], key=natural_sort)
        if t1 != '' and t2 != '':
            bond_type = ('-'.join((t1, t2)))
            if bond_type not in bond_type_params:
                bond_type_params[bond_type] = bond.type

    # Set the hoomd parameters
    harmonic_bond = hoomd.md.bond.harmonic()
    for name, bond_type in bond_type_params.items():
        harmonic_bond.bond_coeff.set(name, 
                k=2 * bond_type.k * ref_distance**2 / ref_energy,
                r0=bond_type.req / ref_distance)

    return harmonic_bond

def _init_hoomd_angles(structure, ref_energy=1.0):
    """ Harmonic angles """
    # Identify the unique angle types before setting
    angle_type_params = {}
    for angle in structure.angles:
        t1, t2, t3 = angle.atom1.type, angle.atom2.type, angle.atom3.type
        t1, t3 = sorted([t1, t3], key=natural_sort)
        angle_type = ('-'.join((t1, t2, t3)))
        if angle_type not in angle_type_params:
            angle_type_params[angle_type] = angle.type

    # set the hoomd parameters
    harmonic_angle = hoomd.md.angle.harmonic()
    for name, angle_type in angle_type_params.items():
        harmonic_angle.angle_coeff.set(name,
                t0=np.deg2rad(angle_type.theteq),
                k=2 * angle_type.k / ref_energy)

    return harmonic_angle

def _init_hoomd_dihedrals(structure, ref_energy=1.0):
    """ Periodic dihedrals (dubbed harmonic dihedrals in HOOMD) """
    # Identify the unique dihedral types before setting
    dihedral_type_params = {}
    for dihedral in structure.structure.dihedrals:
        t1, t2 = dihedral.atom1.type, dihedral.atom2.type
        t3, t4 = dihedral.atom3.type, dihedral.atom4.type
        if [t2, t3] == sorted([t2, t3], key=natural_sort):
            dihedral_type = ('-'.join((t1, t2, t3, t4)))
        else:
            dihedral_type = ('-'.join((t4, t3, t2, t1)))
        if dihedral_type not in dihedral_type_params:
            dihedral_type_params[dihedral_type] = dihedral.type

    # Set the hoomd parameters
    periodic_torsion = hoomd.md.dihedral.harmonic() # These are periodic torsions
    for name, dihedral_type in dihedral_type_params.items():
        if dihedral_type.phase > 0.0001:
            warnings.warn("Dihedral type {} detected with " + 
                    "non-zero phase shift {} ".format(dihedral_type.phae) + 
                    "this is not currently supported in HOOMD, " +
                    "will ignore")
        else:
            periodic_torsion.dihedral_coeff.set(name,
                    k=2*dihedral_type.phi_k / ref_energy,
                    d=1,
                    n=dihedral_type.per)

    return periodic_torsion

def _init_hoomd_rb_torsions(structure, ref_energy=1.0):
    """ RB dihedrals (implemented as OPLS dihedrals in HOOMD) """
    # Identify the unique dihedral types before setting
    dihedral_type_params = {}
    for dihedral in structure.rb_torsions:
        t1, t2 = dihedral.atom1.type, dihedral.atom2.type
        t3, t4 = dihedral.atom3.type, dihedral.atom4.type
        if [t2, t3] == sorted([t2, t3], key=natural_sort):
            dihedral_type = ('-'.join((t1, t2, t3, t4)))
        else:
            dihedral_type = ('-'.join((t4, t3, t2, t1)))
        if dihedral_type not in dihedral_type_params:
            dihedral_type_params[dihedral_type] = dihedral.type

    # Set the hoomd parameter
    rb_torsion = hoomd.md.dihedral.opls()
    for name, dihedral_type in dihedral_type_params.items():
        F_coeffs = RB_to_OPLS(dihedral_type.c0 / ref_energy,
                            dihedral_type.c1 / ref_energy,
                            dihedral_type.c2 / ref_energy,
                            dihedral_type.c3 / ref_energy,
                            dihedral_type.c4 / ref_energy,
                            dihedral_type.c5 / ref_energy)
        rb_torsion.dihedral_coeff.set(name, k1=F_coeffs[0],
                k2=F_coeffs[1], k3=F_coeffs[2], k4=F_coeffs[3])

    return rb_torsion
