import { Routes, Route, Navigate } from 'react-router-dom';
import Layout from './components/layout/Layout';
import Dashboard from './pages/Dashboard';
import Channels from './pages/Channels';
import Bridge from './pages/Bridge';
import Queue from './pages/Queue';
import Commands from './pages/Commands';
import Automation from './pages/Automation';
import Workflows from './pages/Workflows';
import Identity from './pages/Identity';
import Plugins from './pages/Plugins';
import Sessions from './pages/Sessions';
import Metrics from './pages/Metrics';
import Settings from './pages/Settings';

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/channels" element={<Channels />} />
        <Route path="/bridge" element={<Bridge />} />
        <Route path="/queue" element={<Queue />} />
        <Route path="/commands" element={<Commands />} />
        <Route path="/automation" element={<Automation />} />
        <Route path="/workflows" element={<Workflows />} />
        <Route path="/identity" element={<Identity />} />
        <Route path="/plugins" element={<Plugins />} />
        <Route path="/sessions" element={<Sessions />} />
        <Route path="/metrics" element={<Metrics />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </Layout>
  );
}

export default App;
