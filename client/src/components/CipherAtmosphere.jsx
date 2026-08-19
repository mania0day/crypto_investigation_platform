import React from 'react';
import CircuitField from './CircuitField';

/**
 * Technical CipherChain atmosphere —
 * live circuit board + professional analysis overlays.
 */
const CipherAtmosphere = () => {
  return (
    <div className="cipher-atmosphere" aria-hidden="true">
      <div className="cipher-circuit-layer">
        <CircuitField />
      </div>

      <div className="cipher-atmosphere-base" />
      <div className="cipher-atmosphere-frost" />
      <div className="cipher-atmosphere-grid" />
      <div className="cipher-atmosphere-scan" />
      <div className="cipher-tech-frame" />
      <div className="cipher-atmosphere-vignette" />
    </div>
  );
};

export default CipherAtmosphere;
