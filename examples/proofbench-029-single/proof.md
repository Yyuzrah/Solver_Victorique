The answer is all positive even integers k.

Necessity follows by taking n=2. The required divisibility is
\[
3\mid 2+2^k.
\]
Since 2+2^k is congruent to 2+(-1)^k modulo 3, this holds only when k is even.

For sufficiency, fix a positive even integer k and define, for every positive integer M,
\[
F(M)=\sum_{i=0}^{M-1}\binom{M-1}{i}^{k}.
\]
We will prove that M divides F(M).

First suppose p is a prime and p^a divides M, with a at least 1. For 0≤i<M, put q=⌊i/p⌋. Separating the factors whose indices are divisible by p in the factorial product gives the exact identity
\[
\binom{M-1}{i}
=\binom{M/p-1}{q}
\prod_{\substack{1\le j\le i\\p\nmid j}}\frac{M-j}{j}.
\]
Every denominator in the remaining product is relatively prime to p^a. Thus it can be inverted modulo p^a, and each factor is congruent to -1. There are i-q such factors, so
\[
\binom{M-1}{i}\equiv
(-1)^{i-q}\binom{M/p-1}{q}\pmod{p^a}.
\]
Equivalently, this congruence follows by multiplying the exact identity by the product of its remaining denominators, reducing modulo p^a, and cancelling that invertible product.

Because k is even, taking kth powers eliminates the sign. As i runs from 0 to M-1, each q from 0 to M/p-1 occurs exactly p times. Summing therefore yields
\[
F(M)\equiv pF(M/p)\pmod{p^a}. \tag{1}
\]
Apply (1) successively to M, M/p, …, M/p^{a-1}, with respective moduli p^a, p^{a-1}, …, p. Multiplying these congruences by 1, p, …, p^{a-1}, respectively, allows them to be combined modulo p^a:
\[
F(M)\equiv pF(M/p)\equiv p^2F(M/p^2)
\equiv\cdots\equiv p^aF(M/p^a)\equiv0\pmod{p^a}.
\]
Consequently, every prime power in the prime factorization of M divides F(M). These prime powers are pairwise relatively prime, so M divides F(M).

Finally, setting M=n+1 proves that the stated expression is an integer for every positive integer n whenever k is even. Together with necessity, this gives exactly k=2,4,6,… .