import numpy as np

import jax

import scico.numpy as snp
from scico import functional, linop, loss, random
from scico.numpy import BlockArray
from scico.optimize import NonLinearPADMM


class TestMisc:
    def setup_method(self, method):
        np.random.seed(12345)
        self.y = jax.device_put(np.random.randn(32, 33).astype(np.float32))
        self.maxiter = 2
        self.ρ = 1e0
        self.μ = 1e0
        self.ν = 1e0
        self.A = linop.Identity(self.y.shape)
        self.f = loss.SquaredL2Loss(y=self.y, A=self.A)
        self.g = functional.DnCNN()
        self.H = lambda x, z: x - z
        self.x0 = snp.zeros(self.A.input_shape, dtype=snp.float32)

    def test_itstat(self):
        itstat_fields = {"Iter": "%d", "Time": "%8.2e"}

        def itstat_func(obj):
            return (obj.itnum, obj.timer.elapsed())

        nlpadmm_ = NonLinearPADMM(
            f=self.f,
            g=self.g,
            H=self.H,
            rho=self.ρ,
            mu=self.μ,
            nu=self.ν,
            x0=self.x0,
            z0=self.x0,
            u0=self.x0,
            maxiter=self.maxiter,
        )
        assert len(nlpadmm_.itstat_object.fieldname) == 4
        assert snp.sum(nlpadmm_.x) == 0.0

        nlpadmm_ = NonLinearPADMM(
            f=self.f,
            g=self.g,
            H=self.H,
            rho=self.ρ,
            mu=self.μ,
            nu=self.ν,
            x0=self.x0,
            z0=self.x0,
            u0=self.x0,
            maxiter=self.maxiter,
            itstat_options={"fields": itstat_fields, "itstat_func": itstat_func, "display": False},
        )
        assert len(nlpadmm_.itstat_object.fieldname) == 2

    def test_callback(self):
        nlpadmm_ = NonLinearPADMM(
            f=self.f,
            g=self.g,
            H=self.H,
            rho=self.ρ,
            mu=self.μ,
            nu=self.ν,
            x0=self.x0,
            z0=self.x0,
            u0=self.x0,
            maxiter=self.maxiter,
        )
        nlpadmm_.test_flag = False

        def callback(obj):
            obj.test_flag = True

        x = nlpadmm_.solve(callback=callback)
        assert nlpadmm_.test_flag


class TestBlockArray:
    def setup_method(self, method):
        np.random.seed(12345)
        self.y = snp.blockarray(
            (
                np.random.randn(32, 33).astype(np.float32),
                np.random.randn(
                    17,
                ).astype(np.float32),
            )
        )
        self.λ = 1e0
        self.maxiter = 1
        self.ρ = 1e0
        self.μ = 1e0
        self.ν = 1e0
        self.A = linop.Identity(self.y.shape)
        self.f = loss.SquaredL2Loss(y=self.y, A=self.A)
        self.g = (self.λ / 2) * functional.L2Norm()
        self.H = lambda x, z: x - z
        self.x0 = snp.zeros(self.A.input_shape, dtype=snp.float32)

    def test_blockarray(self):
        nlpadmm_ = NonLinearPADMM(
            f=self.f,
            g=self.g,
            H=self.H,
            rho=self.ρ,
            mu=self.μ,
            nu=self.ν,
            x0=self.x0,
            z0=self.x0,
            u0=self.x0,
            maxiter=self.maxiter,
        )
        x = nlpadmm_.solve()
        assert isinstance(x, BlockArray)


class TestReal:
    def setup_method(self, method):
        np.random.seed(12345)
        N = 8
        MB = 10
        # Set up arrays for problem argmin (1/2) ||A x - y||_2^2 + (λ/2) ||B x||_2^2
        Amx = np.diag(np.random.randn(N).astype(np.float32))
        Bmx = np.random.randn(MB, N).astype(np.float32)
        y = np.random.randn(N).astype(np.float32)
        λ = 1e0
        self.Amx = Amx
        self.Bmx = Bmx
        self.y = jax.device_put(y)
        self.λ = λ
        # Solution of problem is given by linear system (A^T A + λ B^T B) x = A^T y
        self.grdA = lambda x: (Amx.T @ Amx + λ * Bmx.T @ Bmx) @ x
        self.grdb = Amx.T @ y

    def test_nlpadmm(self):
        maxiter = 200
        ρ = 1e0
        μ = 5e1
        ν = 1e0
        A = linop.Diagonal(snp.diag(self.Amx))
        f = loss.SquaredL2Loss(y=self.y, A=A)
        g = (self.λ / 2) * functional.SquaredL2Norm()
        C = linop.MatrixOperator(self.Bmx)
        H = lambda x, z: C(x) - z
        nlpadmm_ = NonLinearPADMM(
            f=f,
            g=g,
            H=H,
            rho=ρ,
            mu=μ,
            nu=ν,
            x0=snp.zeros(A.input_shape, dtype=snp.float32),
            z0=snp.zeros(C.output_shape, dtype=snp.float32),
            u0=snp.zeros(C.output_shape, dtype=snp.float32),
            maxiter=maxiter,
        )
        x = nlpadmm_.solve()
        assert (snp.linalg.norm(self.grdA(x) - self.grdb) / snp.linalg.norm(self.grdb)) < 1e-4


class TestComplex:
    def setup_method(self, method):
        N = 8
        MB = 10
        # Set up arrays for problem argmin (1/2) ||A x - y||_2^2 + (λ/2) ||B x||_2^2
        Amx, key = random.randn((N,), dtype=np.complex64, key=None)
        Amx = snp.diag(Amx)
        Bmx, key = random.randn((MB, N), dtype=np.complex64, key=key)
        y, key = random.randn((N,), dtype=np.complex64, key=key)
        λ = 1e0
        self.Amx = Amx
        self.Bmx = Bmx
        self.y = jax.device_put(y)
        self.λ = λ
        # Solution of problem is given by linear system (A^T A + λ B^T B) x = A^T y
        self.grdA = lambda x: (Amx.conj().T @ Amx + λ * Bmx.conj().T @ Bmx) @ x
        self.grdb = Amx.conj().T @ y

    def test_nlpadmm(self):
        maxiter = 300
        ρ = 1e0
        μ = 3e1
        ν = 1e0
        A = linop.Diagonal(snp.diag(self.Amx))
        f = loss.SquaredL2Loss(y=self.y, A=A)
        g = (self.λ / 2) * functional.SquaredL2Norm()
        C = linop.MatrixOperator(self.Bmx)
        H = lambda x, z: C(x) - z
        nlpadmm_ = NonLinearPADMM(
            f=f,
            g=g,
            H=H,
            rho=ρ,
            mu=μ,
            nu=ν,
            x0=snp.zeros(A.input_shape, dtype=snp.complex64),
            z0=snp.zeros(C.output_shape, dtype=snp.complex64),
            u0=snp.zeros(C.output_shape, dtype=snp.complex64),
            maxiter=maxiter,
        )
        x = nlpadmm_.solve()
        assert (snp.linalg.norm(self.grdA(x) - self.grdb) / snp.linalg.norm(self.grdb)) < 1e-4