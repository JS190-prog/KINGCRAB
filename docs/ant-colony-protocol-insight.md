# Ant-colony protocol insight

## Source thesis

The Medium essay [같은 종이어야 군체가 된다](https://medium.com/@ryongkoon1984/%EA%B0%99%EC%9D%80-%EC%A2%85%EC%9D%B4%EC%96%B4%EC%95%BC-%EA%B5%B0%EC%B2%B4%EA%B0%80-%EB%90%9C%EB%8B%A4-0bd60a5e0be7)
argues that a colony emerges when members interpret the same signals and respond
with compatible rules. Different interpretation systems create a negotiation
structure instead of automatic cooperation.

## Biological boundary

"Same species" is not by itself sufficient in real ants. Colonies of the same
species can discriminate against one another using colony-specific cuticular
hydrocarbon profiles. Recognition can also be distributed across workers, while
construction can be coordinated through stigmergic and template interactions.

Primary research:

- [Ants Discriminate Between Different Hydrocarbon Concentrations](https://www.frontiersin.org/journals/ecology-and-evolution/articles/10.3389/fevo.2015.00133/full)
- [Distributed nestmate recognition in ants](https://pmc.ncbi.nlm.nih.gov/articles/PMC4426612/)
- [Stigmergic construction and topochemical information shape ant nest architecture](https://pubmed.ncbi.nlm.nih.gov/26787857/)

## CrabAgent transfer

The engineering unit is therefore not "the same LLM". It is a shared contract:

1. Colony protocol version
2. Event schema and status semantics
3. Task input and output contracts
4. Ontology grammar and evidence interpretation
5. Artifact and tool-receipt envelopes

The initial Codex-only adapter reduces translation and negotiation cost. A future model may join
only through an adapter that passes this semantic compatibility handshake. This
keeps the public product a CrabAgent system; no insect terminology is exposed in
the runtime UX.
