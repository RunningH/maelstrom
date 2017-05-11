#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from dolfin import (
    parameters, XDMFFile, Measure, FunctionSpace, Expression, triangle, begin,
    end, SubMesh, project, Function, assemble, grad, as_vector, File,
    DOLFIN_EPS, info
    )
import matplotlib.pyplot as plt
import numpy
from numpy import pi, sin, cos

import maelstrom.maxwell as cmx

import problems

# We need to allow extrapolation here since otherwise, the equation systems
# for Maxwell cannot be constructed: They contain the velocity `u` (from
# Navier-Stokes) which is only defined on the workpiece subdomain.
# Cf. <https://answers.launchpad.net/dolfin/+question/210508>.
parameters['allow_extrapolation'] = True


def _show_currentloop_field():
    '''http://www.netdenizen.com/emagnettest/offaxis/?offaxisloop
    '''
    from numpy import sqrt

    r = numpy.linspace(0.0, 3.0, 51)
    z = numpy.linspace(-1.0, 1.0, 51)
    R, Z = numpy.meshgrid(r, z)

    a = 1.0
    V = 230 * sqrt(2.0)
    rho = 1.535356e-08
    II = V/rho
    mu0 = pi * 4e-7

    alpha = R / a
    beta = Z / a
    gamma = Z / R
    Q = (1+alpha)**2 + beta**2
    k = sqrt(4*alpha / Q)

    from scipy.special import ellipk
    from scipy.special import ellipe
    Kk = ellipk(k**2)
    Ek = ellipe(k**2)

    B0 = mu0*II / (2*a)

    V = B0 / (pi*sqrt(Q)) \
        * (Ek * (1.0 - alpha**2 - beta**2)/(Q - 4*alpha) + Kk)
    U = B0 * gamma / (pi*sqrt(Q)) \
        * (Ek * (1.0 + alpha**2 + beta**2)/(Q - 4*alpha) - Kk)

    Q = plt.quiver(R, Z, U, V)
    plt.quiverkey(
            Q, 0.7, 0.92, 1e4, '$1e4$',
            labelpos='W',
            fontproperties={'weight': 'bold'},
            color='r'
            )
    plt.show()
    return


def _convert_to_complex(A):
    '''Convert from the format

         [ Re(A) -Im(A) ]
         [ Im(A)  Re(A) ]

    or the format

         [ Re(A)  Im(A) ]
         [ Im(A) -Re(A) ]

    into proper complex-valued format.
    '''
    m, n = A.shape
    assert(m == n)

    # Prepare index sets.
    I0 = numpy.array(range(0, n, 2))
    I1 = numpy.array(range(1, n, 2))

    # <http://stackoverflow.com/questions/7609108/slicing-sparse-scipy-matrix>
    ReA0 = A[I0[:, numpy.newaxis], I0]
    ReA1 = A[I1[:, numpy.newaxis], I1]

    # Make sure those are equal
    diffA = ReA0 - ReA1
    alpha = numpy.sqrt(numpy.vdot(diffA.data, diffA.data))
    diffB = ReA0 + ReA1
    beta = numpy.sqrt(numpy.vdot(diffB.data, diffB.data))
    if alpha < 1.0e-10:
        ReA = ReA0
    elif beta < 1.0e-10:
        ReA = ReA0
    else:
        raise ValueError('||ReA0 - ReA1||_fro = %e' % alpha)

    ImA0 = A[I0[:, numpy.newaxis], I1]
    ImA1 = A[I1[:, numpy.newaxis], I0]

    diffA = ImA0 + ImA1
    diffB = ImA0 - ImA1
    alpha = numpy.sqrt(numpy.vdot(diffA.data, diffA.data))
    beta = numpy.sqrt(numpy.vdot(diffB.data, diffB.data))
    if alpha < 1.0e-10:
        ImA = -ImA0
    elif beta < 1.0e-10:
        ImA = ImA0
    else:
        raise ValueError('||ImA0 - ImA1||_fro = %e' % alpha)
    # Now form the complex-valued matrix.
    return ReA + 1j * ImA


def _pyamg_test(V, dx, ds, Mu, Sigma, omega, coils):
    import pyamg
    import krypy
    import scipy.sparse
    from maelstrom.solver_diagnostics import solver_diagnostics

    # Only calculate in one coil.
    v_ref = 1.0
    voltages_list = [{coils[0]['rings'][0]: v_ref}]

    A, P, b_list, M, W = cmx._build_system(V, dx,
                                           Mu, Sigma,  # dictionaries
                                           omega,
                                           voltages_list,  # dictionary
                                           convections={},
                                           bcs=[]
                                           )

    # Convert the matrix and rhs into scipy objects.
    rows, cols, values = A.data()
    A = scipy.sparse.csr_matrix((values, cols, rows))

    rows, cols, values = P.data()
    P = scipy.sparse.csr_matrix((values, cols, rows))

    # b = b_list[0].array()
    # b = b.reshape(M, 1)

    Ac = _convert_to_complex(A)
    Pc = _convert_to_complex(P)

    parameter_sweep = False
    if parameter_sweep:
        # Find good AMG parameters for P.
        solver_diagnostics(
                Pc,
                fname='results/my_maxwell_solver_diagnostic',
                # definiteness='positive',
                # symmetry='hermitian'
                )

    # Do a MINRES iteration for P^{-1}A.
    # Create solver
    ml = pyamg.smoothed_aggregation_solver(
        Pc,
        strength=('symmetric', {'theta': 0.0}),
        smooth=(
            'energy',
            {
                'weighting': 'local',
                'krylov': 'cg',
                'degree': 2,
                'maxiter': 3
            }
            ),
        Bimprove='default',
        aggregate='standard',
        presmoother=(
            'block_gauss_seidel',
            {'sweep': 'symmetric', 'iterations': 1}
            ),
        postsmoother=(
            'block_gauss_seidel',
            {'sweep': 'symmetric', 'iterations': 1}
            ),
        max_levels=25,
        max_coarse=300,
        coarse_solver='pinv'
        )

    def _apply_inverse_prec_exact(rhs):
        x_init = numpy.zeros((n, 1), dtype=complex)
        out = krypy.linsys.cg(Pc, rhs, x_init,
                              tol=1.0e-13,
                              M=ml.aspreconditioner(cycle='V')
                              )
        if out['info'] != 0:
            info('Preconditioner did not converge; last residual: %g'
                 % out['relresvec'][-1]
                 )
        # # Forget about the cycle used to gauge the residual norm.
        # self.tot_amg_cycles += [len(out['relresvec']) - 1]
        return out['xk']

    # Test preconditioning with approximations of P^{-1}, i.e., systems with
    # P are solved with k number of AMG cycles.
    Cycles = [1, 2, 5, 10]
    ch = plt.cm.get_cmap('cubehelix')
    # Construct right-hand side.
    m, n = Ac.shape
    b = numpy.random.rand(n) + 1j * numpy.random.rand(n)
    for k, cycles in enumerate(Cycles):
        def _apply_inverse_prec_cycles(rhs):
            x_init = numpy.zeros((n, 1), dtype=complex)
            x = numpy.empty((n, 1), dtype=complex)
            residuals = []
            x[:, 0] = ml.solve(rhs,
                               x0=x_init,
                               maxiter=cycles,
                               tol=0.0,
                               accel=None,
                               residuals=residuals
                               )
            # # Alternative for one cycle:
            # amg_prec = ml.aspreconditioner( cycle='V' )
            # x = amg_prec * rhs
            return x

        prec = scipy.sparse.linalg.LinearOperator(
                (n, n),
                _apply_inverse_prec_cycles,
                # _apply_inverse_prec_exact,
                dtype=complex
                )
        out = krypy.linsys.gmres(
                Ac, b,
                M=prec,
                maxiter=100,
                tol=1.0e-13,
                explicit_residual=True
                )
        info(cycles)
        # a lpha = float(cycles-1) / max(Cycles)
        alpha = float(k) / len(Cycles)
        plt.semilogy(
                out['relresvec'], '.-',
                label=cycles,
                color=ch(alpha)
                # color = '%e' % alpha
                )
    plt.legend(title='Number of AMG cycles for P^{~1}')
    plt.title('GMRES convergence history for P^{~1}A (%d x %d)' % Ac.shape)
    plt.show()
    return


def test():
    problem = problems.Crucible()

    from dolfin import plot, interactive
    plot(problem.submesh_workpiece)
    interactive()
    exit(1)

    # The voltage is defined as
    #
    #     v(t) = Im(exp(i omega t) v)
    #          = Im(exp(i (omega t + arg(v)))) |v|
    #          = sin(omega t + arg(v)) |v|.
    #
    # Hence, for a lagging voltage, arg(v) needs to be negative.
    voltages = [
        38.0 * numpy.exp(-1j * 2*pi * 2 * 70.0/360.0),
        38.0 * numpy.exp(-1j * 2*pi * 1 * 70.0/360.0),
        38.0 * numpy.exp(-1j * 2*pi * 0 * 70.0/360.0),
        25.0 * numpy.exp(-1j * 2*pi * 0 * 70.0/360.0),
        25.0 * numpy.exp(-1j * 2*pi * 1 * 70.0/360.0)
        ]
    #
    # voltages = [0.0, 0.0, 0.0, 0.0, 0.0]
    #
    # voltages = [
    #     25.0 * numpy.exp(-1j * 2*pi * 2 * 70.0/360.0),
    #     25.0 * numpy.exp(-1j * 2*pi * 1 * 70.0/360.0),
    #     25.0 * numpy.exp(-1j * 2*pi * 0 * 70.0/360.0),
    #     38.0 * numpy.exp(-1j * 2*pi * 0 * 70.0/360.0),
    #     38.0 * numpy.exp(-1j * 2*pi * 1 * 70.0/360.0)
    #     ]
    #
    # voltages = [
    #     38.0 * numpy.exp(+1j * 2*pi * 2 * 70.0/360.0),
    #     38.0 * numpy.exp(+1j * 2*pi * 1 * 70.0/360.0),
    #     38.0 * numpy.exp(+1j * 2*pi * 0 * 70.0/360.0),
    #     25.0 * numpy.exp(+1j * 2*pi * 0 * 70.0/360.0),
    #     25.0 * numpy.exp(+1j * 2*pi * 1 * 70.0/360.0)
    #     ]

    info('Input voltages:')
    info('%r' % voltages)

    # Merge coil rings with voltages.
    coils = []
    for coil_domain, voltage in zip(problem.coil_domains, voltages):
        coils.append({
            'rings': coil_domain,
            'c_type': 'voltage',
            'c_value': voltage
            })

    subdomain_indices = problem.subdomain_materials.keys()

    background_temp = 1500.0

    # Build subdomain parameter dictionaries.
    mu = {}
    sigma = {}
    for i in subdomain_indices:
        # Take all parameters at background_temp.
        material = problem.subdomain_materials[i]
        mu[i] = material['magnetic permeability'](background_temp)
        sigma[i] = material['electrical conductivity'](background_temp)

    dx = Measure('dx')[problem.subdomains]
    # boundaries = mesh.domains().facet_domains()
    ds = Measure('ds')[problem.subdomains]

    # Function space for Maxwell.
    V = FunctionSpace(problem.mesh, 'CG', 1)

    omega = 240

    # AMG playground.
    pyamg_test = False
    if pyamg_test and parameters['linear_algebra_backend'] == 'uBLAS':
        _pyamg_test(V, dx, ds, mu, sigma, omega, coils)
        exit()

    # TODO when projected onto submesh, the time harmonic solver bails out
    # V_submesh = FunctionSpace(problem.submesh_workpiece, 'CG', 2)
    # u_1 = Function(V_submesh * V_submesh)
    # u_1.vector().zero()
    # conv = {problem.wpi: u_1}

    conv = {}

    Phi, voltages = cmx.compute_potential(
            coils,
            V,
            dx, ds,
            mu, sigma, omega,
            convections=conv
            )

    # # show current in the first ring of the first coil
    # ii = coils[0]['rings'][0]
    # submesh_coil = SubMesh(mesh, subdomains, ii)
    # V1 = FunctionSpace(submesh_coil, 'CG', ii)

    # #File('results/phi.xdmf') << project(as_vector((Phi_r, Phi_i)), V*V)
    from dolfin import plot
    plot(Phi[0], title='Re(Phi)')
    plot(Phi[1], title='Im(Phi)')
    # plot(project(Phi_r, V1), title='Re(Phi)')
    # plot(project(Phi_i, V1), title='Im(Phi)')
    # interactive()

    check_currents = False
    if check_currents:
        r = Expression('x[0]', degree=1, cell=triangle)
        begin('Currents computed after the fact:')
        k = 0
        for coil in coils:
            for ii in coil['rings']:
                J_r = sigma[ii] * (voltages[k].real/(2*pi*r) + omega * Phi[1])
                J_i = sigma[ii] * (voltages[k].imag/(2*pi*r) - omega * Phi[0])
                alpha = assemble(J_r * dx(ii))
                beta = assemble(J_i * dx(ii))
                info('J = %e + i %e' % (alpha, beta))
                info(
                    '|J|/sqrt(2) = %e' % numpy.sqrt(0.5 * (alpha**2 + beta**2))
                    )
                submesh = SubMesh(problem.mesh, problem.subdomains, ii)
                V1 = FunctionSpace(submesh, 'CG', 1)
                # Those projections may take *very* long.
                # TODO find out why
                j_v1 = [
                    project(J_r, V1),
                    project(J_i, V1)
                    ]
                # plot(j_v1[0], title='j_r')
                # plot(j_v1[1], title='j_i')
                # interactive()
                File('results/j%d.xdmf' % ii) << \
                    project(as_vector(j_v1), V1*V1)
                k += 1
        end()

    show_phi = True
    if show_phi:
        filename = './results/phi.xdmf'
        info('Writing out Phi to %s...' % filename)
        phi_file = XDMFFile(filename)
        phi_file.parameters['rewrite_function_mesh'] = False
        phi_file.parameters['flush_output'] = True
        phi = Function(V, name='phi')
        Phi0 = project(Phi[0], V)
        Phi1 = project(Phi[1], V)
        for t in numpy.linspace(0.0, 2*pi/omega, num=100, endpoint=False):
            # Im(Phi * exp(i*omega*t))
            phi.vector().zero()
            phi.vector().axpy(sin(omega*t), Phi0.vector())
            phi.vector().axpy(cos(omega*t), Phi1.vector())
            phi_file << (phi, t)

    show_magnetic_field = True
    if show_magnetic_field:
        # Show the resulting magnetic field
        #
        #   B_r = -dphi/dz,
        #   B_z = 1/r d(rphi)/dr.
        #
        r = Expression('x[0]', degree=1, cell=triangle)
        g = 1/r * grad(r*Phi[0])
        B_r = project(as_vector((-g[1], g[0])), V*V)
        g = 1/r * grad(r*Phi[1])
        B_i = project(as_vector((-g[1], g[0])), V*V)
        filename = './results/magnetic-field.xdmf'
        info('Writing out B to %s...' % filename)
        b_file = XDMFFile(filename)
        b_file.parameters['rewrite_function_mesh'] = False
        b_file.parameters['flush_output'] = True
        B = Function(V*V, name='magnetic field B')
        if abs(omega) < DOLFIN_EPS:
            plot(B_r, title='Re(B)')
            plot(B_i, title='Im(B)')
            B.assign(B_r)
            b_file << B
            interactive()
        else:
            # Write those out to a file.
            for t in numpy.linspace(0.0, 2*pi/omega, num=100, endpoint=False):
                # Im(B * exp(i*omega*t))
                B.vector().zero()
                B.vector().axpy(sin(omega*t), B_r.vector())
                B.vector().axpy(cos(omega*t), B_i.vector())
                b_file << (B, t)

    if problem.wpi:
        # Get resulting Lorentz force.
        lorentz = {
            problem.wpi: cmx.compute_lorentz(Phi, omega, sigma[problem.wpi])
            }
        # Show the Lorentz force.
        L = FunctionSpace(problem.submesh_workpiece, 'CG', 1)
        # TODO find out why the projection here segfaults
        lfun = Function(L*L, name='Lorentz force')
        lfun.assign(project(lorentz[problem.wpi], L*L))
        filename = './results/lorentz.xdmf'
        info('Writing out Lorentz force to %s...' % filename)
        lorentz_file = File(filename)
        lorentz_file << lfun
        filename = './results/lorentz.pvd'
        info('Writing out Lorentz force to %s...' % filename)
        lorentz_file = File(filename)
        lorentz_file << lfun
        plot(lfun, title='Lorentz force')
        interactive()

    # # Get resulting Joule heat source.
    # #ii = None
    # ii = 4
    # if ii in subdomain_indices:
    #     joule = cmx.compute_joule(V, dx,
    #                               Phi, voltages,
    #                               omega, sigma, mu,
    #                               wpi,
    #                               subdomain_indices
    #                               )
    #     submesh = SubMesh(mesh, subdomains, ii)
    #     V_submesh = FunctionSpace(submesh, 'CG', 1)
    #     # TODO find out why the projection here segfaults
    #     jp = project(joule[ii], V_submesh)
    #     jp.name = 'Joule heat source'
    #     filename = './results/joule.xdmf'
    #     joule_file = XDMFFile(filename)
    #     joule_file << jp
    #     plot(jp, title='heat source')
    #     interactive()

    # # For the lulz: solve heat equation with the Joule source.
    # u = TrialFunction(V)
    # v = TestFunction(V)
    # a = zero() * dx(0)
    # r = Expression('x[0]', degree=1, cell=triangle)
    # # v/r doesn't hurt: hom. dirichlet boundary for r=0.
    # for i in subdomain_indices:
    #     a += dot(kappa[i] * r * grad(u), grad(v/r)) * dx(i)
    # sol = Function(V)
    # class OuterBoundary(SubDomain):
    #     def inside(self, x, on_boundary):
    #         return on_boundary and abs(x[0]) > DOLFIN_EPS
    # outer_boundary = OuterBoundary()
    # bcs = DirichletBC(V, background_temp, outer_boundary)
    # solve(a == joule, sol, bcs=bcs)
    # plot(sol)
    # interactive()
    return


if __name__ == '__main__':
    test()
